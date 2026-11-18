# file: src/servicedesk/models/ticket.py
"""工单结构：把模型吐出的「一段文字」变成可校验、可入库的对象。

三个核心能力：
1. 用 pydantic 声明结构与规则（类型、长度、范围、枚举、跨字段）
2. 用 model_json_schema() 生成函数调用用的 schema（2.1 直接用）
3. 解析失败时，把 ValidationError 翻译成「能喂回给模型」的纠错提示

设计原则：**校验失败不是异常，而是一次对话机会**。
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


# ------------------------------- 枚举与别名 -------------------------------


class Priority(str, Enum):
    """优先级。P0 最高。"""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class Category(str, Enum):
    """工单分类。"""

    ACCOUNT = "account"
    NETWORK = "network"
    HARDWARE = "hardware"
    SOFTWARE = "software"
    OTHER = "other"


# 模型很爱输出中文/口语化的值。与其让它报错重试，不如先归一化。
PRIORITY_ALIASES: dict[str, Priority] = {
    "紧急": Priority.P0,
    "特急": Priority.P0,
    "最高": Priority.P0,
    "高": Priority.P1,
    "重要": Priority.P1,
    "中": Priority.P2,
    "普通": Priority.P2,
    "低": Priority.P3,
    "不急": Priority.P3,
}

CATEGORY_ALIASES: dict[str, Category] = {
    "账户": Category.ACCOUNT,
    "账号": Category.ACCOUNT,
    "权限": Category.ACCOUNT,
    "网络": Category.NETWORK,
    "硬件": Category.HARDWARE,
    "设备": Category.HARDWARE,
    "软件": Category.SOFTWARE,
    "其他": Category.OTHER,
}


# -------------------------------- 主模型 --------------------------------


class Ticket(BaseModel):
    """一条结构化后的 IT 工单。"""

    # extra="forbid"：多一个字段就报错。
    # 这看起来"太严"，但它能让模型瞎编的字段立刻暴露，而不是静默丢弃。
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    ticket_id: str = Field(
        pattern=r"^T-\d{4,}$",
        description="工单编号，形如 T-1001",
        examples=["T-1001"],
    )
    title: str = Field(min_length=4, max_length=80, description="一句话标题")
    summary: str = Field(min_length=10, max_length=500, description="问题描述")
    priority: Priority = Field(description="优先级 P0–P3")
    category: Category = Field(description="分类")
    affected_system: str = Field(min_length=2, max_length=40, description="受影响的系统")
    reporter: str = Field(min_length=2, max_length=40, description="报障人")
    tags: list[str] = Field(default_factory=list, max_length=5, description="最多 5 个标签")
    confidence: float = Field(ge=0.0, le=1.0, description="模型对本次结构化的置信度")
    needs_approval: bool = Field(default=False, description="是否需要人工审批")
    created_at: datetime | None = Field(default=None, description="创建时间，ISO 8601")

    # -- 归一化：把"能猜到意思"的输入变成合法值 --

    @field_validator("priority", mode="before")
    @classmethod
    def _normalize_priority(cls, value: object) -> object:
        if isinstance(value, Priority):
            return value
        if isinstance(value, str):
            text = value.strip()
            upper = text.upper()
            if upper in Priority.__members__:
                return Priority[upper]
            if text in PRIORITY_ALIASES:
                return PRIORITY_ALIASES[text]
        return value  # 交给 pydantic 报错，别在这里吞掉

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, value: object) -> object:
        if isinstance(value, Category):
            return value
        if isinstance(value, str):
            text = value.strip()
            lower = text.lower()
            if lower in Category.__members__.values():
                return Category(lower)
            if text in CATEGORY_ALIASES:
                return CATEGORY_ALIASES[text]
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, value: object) -> object:
        """模型有时给 "a,b,c" 或 "a、b"，统一成列表。"""
        if isinstance(value, str):
            for sep in ("，", "、", ";", "；"):
                value = value.replace(sep, ",")
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    # -- 跨字段规则：schema 描述不了的，只能靠代码 --

    @model_validator(mode="after")
    def _p0_requires_approval(self) -> Ticket:
        if self.priority is Priority.P0 and not self.needs_approval:
            raise ValueError(
                "priority 为 P0 时必须同时给出 needs_approval=true"
                "（P0 代表线上事故，任何处置动作都要人工审批）"
            )
        return self


# ------------------------------ 错误与纠错 ------------------------------


class TicketParseError(ValueError):
    """解析失败。携带可直接回喂给模型的修复提示。"""

    def __init__(
        self,
        stage: str,
        *,
        detail: str = "",
        raw: str = "",
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(detail or stage)
        self.stage = stage          # "not_json" | "invalid"
        self.detail = detail
        self.raw = raw
        self.errors = errors or []

    @property
    def repair_message(self) -> str:
        """一段可以直接塞回 messages 的纠错提示。"""
        if self.stage == "not_json":
            return (
                "你上一条回复里没有可解析的 JSON 对象。"
                "请只输出一个 JSON 对象，不要解释、不要 markdown 代码块、不要前后缀文字。"
            )

        lines = [
            f"你上一次输出的 JSON 未通过结构校验，共 {len(self.errors)} 处问题：",
        ]
        for index, error in enumerate(self.errors, start=1):
            location = ".".join(str(part) for part in error.get("loc") or ())
            where = f"字段 `{location}`" if location else "整体规则"
            # pydantic 会给自定义 ValueError 加 "Value error, " 前缀，去掉更好读
            message = str(error.get("msg", "")).removeprefix("Value error, ")
            if error.get("type") == "missing":
                received = "（未提供）"      # 缺字段时 input 是整个对象，展示它只会干扰
            else:
                received = _short_repr(error.get("input"))
            lines.append(f"{index}. {where}：{message}（你给的值：{received}）")
        lines.append("")
        lines.append(
            "必须满足的额外规则：priority 为 P0 时，needs_approval 必须为 true。"
        )
        lines.append("请只输出修正后的完整 JSON 对象，不要解释。")
        return "\n".join(lines)


def _short_repr(value: object, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


# ------------------------------ 解析管线 ------------------------------


def strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 包裹。模型很爱加这个。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


def extract_json_object(text: str) -> str | None:
    """从"一段话里夹着 JSON"的文本中，抠出第一个完整的 JSON 对象。

    用括号配对而不是正则：正则处理不了嵌套，也分不清字符串里的花括号
    （比如 title 里写了 "{紧急}"）。
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def load_ticket(raw: str) -> Ticket:
    """把模型的原始输出变成 Ticket；失败抛 TicketParseError（带修复提示）。

    三步走：去代码块 → 抠 JSON → 校验。
    """
    cleaned = strip_code_fence(raw)
    candidate = extract_json_object(cleaned)
    if candidate is None:
        raise TicketParseError("not_json", raw=raw, detail="输出中找不到 JSON 对象")

    try:
        return Ticket.model_validate_json(candidate)
    except ValidationError as exc:
        raise TicketParseError("invalid", raw=raw, errors=exc.errors()) from exc


# ---------------------------- 给函数调用用的 schema ----------------------------


def ticket_tool_schema() -> dict[str, Any]:
    """OpenAI 风格的 function 定义，2.1 直接塞进 tools 里用。

    parameters 就是 pydantic 的 model_json_schema()——结构与校验规则
    会自动同步，改模型就等于改 schema，不会漏改。
    """
    return {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": "把用户描述的结构化成一个 IT 工单",
            "parameters": Ticket.model_json_schema(),
        },
    }


def pretty_schema_summary() -> str:
    """打印 schema 的骨架，避免把几百行 JSON 全糊到屏幕上。"""
    schema = Ticket.model_json_schema()
    required = schema.get("required", [])
    props = schema.get("properties", {})
    lines = [f"顶层字段 {len(props)} 个，其中必填 {len(required)} 个："]
    for name, spec in props.items():
        marker = "必填" if name in required else "可选"
        ref = spec.get("$ref") or spec.get("type") or spec.get("anyOf")
        default = f"，默认 {json.dumps(spec['default'], ensure_ascii=False)}" if "default" in spec else ""
        lines.append(f"  - {name} [{marker}] {ref}{default}")
    defs = schema.get("$defs", {})
    if defs:
        lines.append(f"$defs 里有 {len(defs)} 个引用定义：{sorted(defs)}")
    return "\n".join(lines)
