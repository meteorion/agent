---
section: "1.4"
title: "用 pydantic v2 把一段文字变成结构化数据"
stage: 1
status: done
depth: L2
difficulty: 2
minutes: 75
milestone: M1
depends_on: ["1.3"]
produces: ["src/servicedesk/models/ticket.py"]
verifies: "喂多余字段/缺字段的 JSON，给出可读纠错而非崩溃"
part: null
updated: "2026-11-17"
---

# 1.4 用 pydantic v2 把一段文字变成结构化数据

## 0. 本节导航

**本节在链路中的位置**：1.3 拿到了 `choices[0].message.content`——一段**字符串**。本节把它变成**带类型的对象**。后面 2.1 的 Function Calling 要用本节的 `model_json_schema()`，1.5 的重试要用本节的 `repair_message`，6.x 入库更离不开它。

**本节你将获得**

1. 用 `BaseModel` + `Field` 声明一份**能被机器校验**的数据契约。
2. 把"模型爱加的 markdown 代码块"和"前后废话"稳定剥掉，抠出真正的 JSON。
3. 用三个 `field_validator` 把"紧急 / 网络 / 逗号分隔标签"这类脏值归一化。
4. 用 `model_validator` 表达 **schema 描述不了**的跨字段规则（P0 必须人工审批）。
5. 把 `ValidationError` 翻译成**模型看得懂、能照着改**的纠错提示——这是本节的核心产出。

**前置检查清单**

- 1.1 的 `uv run python -m servicedesk` 通过。
- 1.3 的 `uv run pytest` 是 15 passed。

**难度**：★★☆☆☆ ｜ **预计时长**：75 分钟 ｜ **本节费用**：**¥0**（纯本地，不调任何 LLM）

---

## 1. 为什么需要它（痛点先行）

你写好了 1.3 的客户端，让它输出一个工单：

```python
result = await client.chat([{"role": "user", "content": "把这段话整理成工单：VPN 连不上…"}])
print(result.text)
```

第一次运行，模型很配合：

```json
{"ticket_id": "T-1001", "title": "VPN 连不上", "priority": "P1", ...}
```

`json.loads` 成功，你很高兴。上线三天后，你收到这些：

**事故 A：**

```
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
```

模型这次回的是：

````
好的，我已经整理好了：
```json
{"ticket_id": "T-1001", ...}
```
还需要调整吗？
````

**事故 B：**

```json
{"ticket_id": "T-1001", "priority": "紧急", "severity": "high"}
```

解析成功，但 `priority` 的值根本不在你的字典里；`severity` 是你从没定义过的字段，默默躺在 `dict` 里，直到某天写库时报 `Unknown column`。

**事故 C：**

```json
{"ticket_id": "T-1001", "title": "VPN 连不上"}
```

解析也成功——但少了 `summary`，你的下游代码 `ticket["summary"]` 直接 `KeyError`。

**这三个事故的共同点：`json.loads` 只保证"是合法 JSON"，不保证"是你要的数据"。**

于是你的代码里长满了这种东西：

```python
priority = data.get("priority", "P2")
if priority in ("P0", "紧急", "特急"): ...
ticket_id = data["ticket_id"]
if not ticket_id.startswith("T-"): ...
```

**每加一个字段，这种胶水代码就长一截，而且没人敢删。** 本节要做的，是把这套东西换成一个声明式的契约。

---

## 2. 核心概念与架构设计

### 2.1 类比：报关单与海关

- **`Ticket` 模型 = 报关单模板**：规定了哪些栏必填、每栏填什么格式、哪几栏之间有联动关系。
- **解析管线 = 报关员**：逐项核对，不通过就退单。
- **`repair_message` = 退单理由**：必须写清"**哪一栏、应该是什么、你填了什么**"。只写"格式错误"的退单理由，会让你和模型来回扯皮十次。

> **这个类比在哪失效**：海关不负责"猜你本来想填什么"。如果模型把 `P0` 写成 `P零`，校验器只能退单，**不会自己修**。想让它"自动修"，得靠归一化别名（§5.2）或者回喂重试（§9.2）——这两件事都得你显式设计。

### 2.2 三层管线

```
模型输出（一段文字）
      │
      │ ① strip_code_fence()      去掉 ```json 包裹、前后废话
      ▼
      │ ② extract_json_object()   括号配对，抠出第一个完整 {...}
      ▼
      │ ③ model_validate_json()   pydantic 逐字段校验 + 归一化
      │
      ├── 通过 ──► Ticket 对象（有类型、有校验、可直接入库）
      │
      └── 失败 ──► TicketParseError
                     └─ .repair_message ──► 回喂模型，让它重说一次
```

**关键认知：第 ③ 步的失败不是"程序出错了"，而是"对话还要继续一轮"。** 把校验失败当成异常崩溃，是新手最常见的做法——它会让整个 Agent 因为模型偶尔手抖而彻底失败。

### 2.3 数据契约长什么样

| 字段 | 类型 | 约束 | 为什么这么约束 |
|---|---|---|---|
| `ticket_id` | `str` | `^T-\d{4,}$` | 下游要按这个格式建索引，错了就查不到 |
| `title` | `str` | 4–80 字 | 太短没信息，太长是模型把整段话塞进来了 |
| `summary` | `str` | 10–500 字 | 防止模型只回一个"同上" |
| `priority` | 枚举 | P0–P3 | 用枚举而不是 str，让非法值在边界就被拦 |
| `category` | 枚举 | 5 类 | 同上 |
| `tags` | `list[str]` | ≤5 个 | 无限标签 = 无限 token |
| `confidence` | `float` | 0–1 | 模型自评置信度，低于阈值可转人工 |
| `needs_approval` | `bool` | 默认 `False` | 与 `priority` 联动（见下） |

### 2.4 宽松模式 vs 严格模式（**本节最容易被忽略的一点**）

pydantic v2 默认是**宽松（lax）**的：`"0.9"` 会被转成 `0.9`，`"true"` 会被转成 `True`。

好处：模型把数字写成字符串时你还能活。
坏处：**它把"模型输出格式不稳"这个信号悄悄吃掉了**。等哪天模型写成 `"很高"`，你才炸，而那时你已经不知道是从哪一天开始不稳的。

实测（§7 案例 4）：

```
confidence : 0.9 (float)      ← 输入是字符串 "0.9"，被静默转换
```

> 生产建议：**关键字段用严格模式**（`Strict(…)` 或 `model_config = ConfigDict(strict=True)`），把"格式漂移"变成显式失败，交给纠错回路去修，而不是让它蒙混过关。

### 2.5 schema 与 Function Calling 的关系（伏笔）

```python
Ticket.model_json_schema()
```

产出的 JSON Schema **就是** OpenAI function calling 里 `parameters` 字段要的东西。也就是说：**你在这里定义的模型，到 2.1 就自动变成模型的"工具说明书"，不用手写第二份。**

这带来了一个非常好的性质：**改模型 = 改 schema = 改模型看到的能力描述**，三者永远不会不同步。

### 2.6 适用范围（边界）

| 场景 | 用 pydantic 模型 | 用裸 dict / 正则 |
|---|---|---|
| 模型输出要入库、要跨模块传递 | ✅ | ❌ |
| 要做工具调用的参数定义 | ✅ 自动生成 schema | ❌ 手写两份会漂移 |
| 一次性的 demo 脚本 | ⚠️ 有点重 | ✅ |
| 输出结构每天都在变 | ⚠️ 别硬编码 | ✅ |

---

## 3. 环境与版本

本节**不新增依赖**——1.1 已经装了 pydantic。

| 组件 | 实测版本 | 说明 |
|---|---|---|
| pydantic | `2.14.3` | v2 与 v1 API **不兼容**，见 §8 坑 1 |
| Python | `3.12.13` | `str.removeprefix` 需要 ≥3.9 |

**验证装对了**

```powershell
uv run python -c "import pydantic; print(pydantic.VERSION)"
```

```
2.14.3
```

---

## 4. 代码实战（4 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/models/__init__.py` | 包标记（一行 docstring） |
| `src/servicedesk/models/ticket.py` | **主角**：模型 + 解析管线 + 纠错 |
| `scripts/ticket_demo.py` | 8 个脏数据案例 |
| `tests/test_ticket.py` | 13 个断言 |

### 4.1 `src/servicedesk/models/ticket.py`（全文）

```python
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
```

### 4.2 `scripts/ticket_demo.py`（8 个案例）

它内置了 8 段"模型输出"字符串，从干净 JSON 到纯自然语言都有。核心循环只有十几行：

```python
    try:
        ticket = load_ticket(raw)
    except TicketParseError as exc:
        print(f"结果: ❌ 被拦下（stage={exc.stage}，{len(exc.errors)} 处问题）")
        print("修复提示（可直接作为下一条 user 消息回喂给模型）:")
        for line in exc.repair_message.splitlines():
            print(f"  {line}")
        return False
```

完整代码见 [scripts/ticket_demo.py](../../servicedesk-agent/scripts/ticket_demo.py)。

### 4.3 `tests/test_ticket.py`（节选：失败路径）

成功路径的测试很直白，真正值钱的是失败路径这几条：

```python
def test_extra_field_is_rejected() -> None:
    with pytest.raises(TicketParseError) as excinfo:
        load_ticket(dump(severity="high"))

    error = excinfo.value
    assert error.stage == "invalid"
    assert any(e["type"] == "extra_forbidden" for e in error.errors)
    assert "severity" in error.repair_message


def test_missing_field_repair_says_not_provided() -> None:
    payload = {k: v for k, v in CLEAN.items() if k != "summary"}
    with pytest.raises(TicketParseError) as excinfo:
        load_ticket(json.dumps(payload, ensure_ascii=False))

    message = excinfo.value.repair_message
    assert "整体对象" not in message
    assert "（未提供）" in message
    assert "summary" in message


def test_p0_requires_approval() -> None:
    with pytest.raises(TicketParseError) as excinfo:
        load_ticket(dump(priority="P0", needs_approval=False))

    message = excinfo.value.repair_message
    assert "needs_approval" in message
    assert "Value error, " not in message, "pydantic 的前缀应该被去掉"
```

完整 13 个用例见 [tests/test_ticket.py](../../servicedesk-agent/tests/test_ticket.py)。

### 4.4 运行

```powershell
uv run python -m scripts.ticket_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 `extra="forbid"`：为什么要"太严"

默认值是 `ignore`——多余字段被**静默丢弃**。看起来宽容，实际很危险：

模型这次多给了 `severity`，你忽略了。三个月后你在排查"为什么高危工单没升级"，才发现它一直在 `severity` 里写着 `high`，而你从来没读过。**静默丢弃 = 悄悄丢失信息。**

`forbid` 把它变成一次显式失败，顺手得到一条纠错提示，模型下一轮就学会了。

代价：如果模型隔三岔五加字段，你会被 400 淹没。所以这条要和 `repair_message` 配套使用——**有纠错回路才敢严。**

### 5.2 三个 `field_validator` 的分工

```python
@field_validator("priority", mode="before")
```

`mode="before"` 的意思是"**在 pydantic 做类型转换之前**先跑"。这很关键：

- `before`：拿到原始值（可能是 `"紧急"`），你可以把它改成 `Priority.P0`。
- `after`：拿到的是已经转换后的值（此时类型必须是 `Priority`，非法值早就报错了）——**你根本没机会救它**。

所以归一化必须写在 `before`。

**注意最后一行的 `return value`**：

```python
        return value  # 交给 pydantic 报错，别在这里吞掉
```

如果这里 `return Priority.P2` 兜底，你就永远收不到"模型给了个没见过的优先级"这个信号。**归一化只处理你确实能猜到的值，其余一律放行让它报错。**

### 5.3 `"0.9"` 被静默转换：宽松模式的账

§7 案例 4 的结果：

```
confidence : 0.9 (float)
```

输入明明是字符串。pydantic 的宽松模式帮你转了。

**这次是好事，下次是灾难。** 因为模型输出的"形状"在悄悄漂移：今天是 `0.9`，明天是 `"0.9"`，后天是 `"很高"`——前两个你都"成功"了，第三个才炸。此时你已经无法定位是从哪天开始不稳的。

两个应对：

```python
# 方案 A：关键字段严格
from pydantic import StrictFloat
confidence: StrictFloat = Field(ge=0.0, le=1.0)

# 方案 B：全局严格（最推荐，但要求模型很稳）
model_config = ConfigDict(strict=True, extra="forbid")
```

**建议**：先用宽松跑通，把它当成"已埋的雷"记在文档里；等有评估体系（阶段五）之后切严格，用指标兜住回归。

### 5.4 `model_validator(mode="after")`：schema 表达不了的规则

`priority` 是 P0、`needs_approval` 是 `false`——这两个字段**各自**都合法，但组合起来违反业务规则。

JSON Schema 表达不了这种跨字段约束（能表达的那部分叫 `if/then/else`，写起来极其丑陋，而且模型基本看不懂）。所以放在代码里：

```python
    @model_validator(mode="after")
    def _p0_requires_approval(self) -> Ticket:
```

`mode="after"` 表示"所有字段都校验完了再跑"。此时 `self.priority` 已经是枚举、`self.needs_approval` 已经是布尔，可以直接比较。

**代价**：这条规则对模型是**不可见**的（它不出现在 schema 里）。所以必须在 `repair_message` 里用自然语言再说一遍：

```python
        lines.append(
            "必须满足的额外规则：priority 为 P0 时，needs_approval 必须为 true。"
        )
```

**这就是"schema 给你结构，repair_message 给你规则"的分工。**

### 5.5 为什么不能直接把 `str(ValidationError)` 喂回去

`str(exc)` 长这样：

```
3 validation errors for Ticket
summary
  Field required [type=missing, input_value={'ticket_id': 'T-1001', '...lse, 'severity': 'high'}, input_type=dict]
confidence
  Input should be less than or equal to 1 [type=less_than_equal, input_value=1.7, input_type=float]
severity
  Extra inputs are not permitted [type=extra_forbidden, input_value='high', input_type=str]
```

问题有三个：

1. **612 字符，其中大半是把整个对象重复打印**（`input_value={...}` 出现了多次）——纯浪费 token。
2. **英文 + pydantic 内部术语**（`less_than_equal`、`extra_forbidden`），模型要额外翻译一遍。
3. **没有"接下来该怎么办"**，模型可能只是原样重发。

我们的 `repair_message` 是 **279 字符**，只保留"字段 + 原因 + 你给的值 + 正确做法"。

> **一句话：错误信息是给模型读的，不是给日志读的。** 两者的最优形态完全不同——日志要全，给模型要准且短。

### 5.6 `$defs` 与 strict 模式（2.1 的伏笔）

实测 schema 摘要：

```
$defs 里有 2 个引用定义：['Category', 'Priority']
```

因为 `Priority` 是枚举，pydantic 把它抽到 `$defs` 里，字段用 `{"$ref": "#/$defs/Priority"}` 引用。

**这在 2.1 会变成真问题**：OpenAI 的 strict function calling 要求 schema 里不能有 `$ref`，必须**内联展开**。两种解法：

- 简单粗暴：把枚举字段改成 `str` + `pattern`（牺牲类型安全，换 schema 平坦）
- 正确做法：写一个 `inline_refs()` 把 `$defs` 递归展开（2.1 会实现）

现在先记住这个坑，别到时候以为是自己的模型写错了。

### 5.7 括号配对比正则强在哪

`extract_json_object()` 里有三个状态：`depth`（嵌套层数）、`in_string`（是否在字符串里）、`escaped`（是否在转义）。

**为什么需要 `in_string`**：`{"title": "VPN{紧急}连不上"}` 里的 `{}` 在字符串内部，**不能算嵌套**。正则会把它算进去（§6.1 有实测）。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：用正则抠 JSON，标题带花括号就断

把 `extract_json_object` 换成最常见的那行正则：

```python
import re
candidate = re.search(r"\{.*?\}", text, re.S).group(0)   # 非贪婪
```

喂一个标题里带花括号的工单，实测：

```
非贪婪匹配: {"ticket_id": "T-1001", "title": "VPN{紧急}
非贪婪 -> json.loads 失败: Unterminated string starting at
```

**它在 `title` 里的第一个 `}` 处截断了**，拿到的是一段残缺 JSON。

换成贪婪匹配 `\{.*\}`？这次碰巧能过（因为文本里只有一对顶层花括号），但只要模型后面再多说一句带花括号的话（"如需撤销请回复{撤销}"），贪婪就会一路吃到最后一个 `}`，同样错。

**括号配对法**：同一输入，长度 `243`（完整），`title = "VPN{紧急}连不上"` 正确。

> 教训：**JSON 是有语法的，不要用正则解析有语法的东西。** 这是"用正则解析 HTML"的同款陷阱。

### 6.2 实验 B：直接回喂 `str(ValidationError)`

把 `repair_message` 换成 `str(exc)`，实测两者长度：

```
str(ValidationError) 长度: 612 字符
repair_message       长度: 279 字符
```

**而且 612 里绝大部分是无效信息**——它把整个输入对象打印了两遍。

放到生产里更贵：假设每天 10 万次调用、其中 5% 需要重试，每次多 333 字符 ≈ 110 token，一天白烧约 550 万 token 的上下文。

> 教训：**给模型的错误信息要"精准且短"，不是"完整且长"。** 完整留给日志。

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.ticket_demo
```

实测输出（节选，完整 8 个案例）：

```
====================================================================
第 0 步：schema 是从模型自动生成的
====================================================================
顶层字段 11 个，其中必填 8 个：
  - ticket_id [必填] string
  - title [必填] string
  - summary [必填] string
  - priority [必填] #/$defs/Priority
  - category [必填] #/$defs/Category
  - affected_system [必填] string
  - reporter [必填] string
  - tags [可选] array
  - confidence [必填] number
  - needs_approval [可选] boolean，默认 false
  - created_at [可选] [{'format': 'date-time', 'type': 'string'}, {'type': 'null'}]，默认 null
$defs 里有 2 个引用定义：['Category', 'Priority']

函数名: create_ticket
必填字段: ['ticket_id', 'title', 'summary', 'priority', 'category', 'affected_system', 'reporter', 'confidence']
```

四个成功案例（1–4）：干净 JSON、markdown 包裹、中文枚举、字符串数字。

一个失败案例的完整输出：

```
====================================================================
案例 6：漏了必填字段 summary
====================================================================
结果: ❌ 被拦下（stage=invalid，1 处问题）
修复提示（可直接作为下一条 user 消息回喂给模型）:
  你上一次输出的 JSON 未通过结构校验，共 1 处问题：
  1. 字段 `summary`：Field required（你给的值：（未提供））
  
  必须满足的额外规则：priority 为 P0 时，needs_approval 必须为 true。
  请只输出修正后的完整 JSON 对象，不要解释。
```

结尾：

```
====================================================================
小结
====================================================================
  8 个案例：4 个成功、4 个被拦下并给出可回喂的修复提示。
  关键点：校验失败不是崩溃，而是「让模型重说一次」的机会。
```

跑测试：

```powershell
uv run pytest
```

```
............................                                             [100%]
28 passed in 2.15s
```

（1.1 的 3 个 + 1.2 的 3 个 + 1.3 的 9 个 + 本节的 13 个。）

---

## 8. 生产环境踩坑实录

### 坑 1：pydantic v1 的写法在 v2 里全废

**现象**

从旧项目复制过来的代码，一跑就是：

```
TypeError: BaseModel.__init__() takes 1 positional argument but 2 were given
```

或者：

```
AttributeError: type object 'Ticket' has no attribute 'parse_obj'
```

**定位方法**

```powershell
uv run python -c "import pydantic; print(pydantic.VERSION)"
```

看到 `2.x` 就说明是 v1→v2 的迁移问题。

**根因**

v2 是重写，核心 API 全改了名：

| v1 写法 | v2 写法 |
|---|---|
| `Ticket.parse_obj(d)` | `Ticket.model_validate(d)` |
| `Ticket.parse_raw(s)` | `Ticket.model_validate_json(s)` |
| `ticket.dict()` | `ticket.model_dump()` |
| `ticket.json()` | `ticket.model_dump_json()` |
| `Field(regex="...")` | `Field(pattern="...")` |
| `@validator("x")` | `@field_validator("x")` |
| `@root_validator` | `@model_validator(mode="after")` |

**修法**

```powershell
# 自动迁移大部分写法
uv run pip install bump-pydantic
uv run bump-pydantic src/
```

**顺手加固**：`pyproject.toml` 里锁 `pydantic>=2.9,<3.0`（1.1 已经这么做了），并且在 CI 里跑一次导入检查——别等线上才发现装成了 v1。

### 坑 2：模型输出 Python 风格的 `True` / `False` / `None`

**现象**

```
json.decoder.JSONDecodeError: Expecting value: line 1 column 42 (char 41)
```

指着的位置是：

```json
{"needs_approval": True}
```

**定位方法**

把出问题的那段原文 `repr()` 出来，看是不是 `True` 而不是 `true`。

**根因**

JSON 标准里只有 `true` / `false` / `null`，**没有** `True` / `False` / `None`。模型见过太多 Python 代码，偶尔会混。

**修法**

在解析前做一次**受控**替换（注意只在结构位置上替换，别动字符串内容）：

```python
import re

def fix_python_literals(text: str) -> str:
    """只修 JSON 结构位置的 True/False/None，不碰字符串内部。"""
    return re.sub(
        r'(?<=[:,\[])\s*(True|False|None)\b',
        lambda m: {"True": "true", "False": "false", "None": "null"}[m.group(1)],
        text,
    )
```

更稳的做法是让模型**别自己拼 JSON**——用 Function Calling（2.1）或结构化输出，由服务端保证格式。

### 坑 3：宽松转换掩盖上游漂移，账单和报警一起失效

**现象**

一切"正常"。三个月后做数据分析，发现 `confidence` 字段的平均值异常稳定在 `0.8`——因为大量 `"0.8"`（字符串）被静默转成了 `0.8`，而真正异常的 `"high"` 被记成了**校验失败**，两部分数据统计口径完全不同。

**定位方法**

在解析层加一个"是否发生过强制转换"的探针：

```python
from pydantic import model_validator

@model_validator(mode="before")
@classmethod
def _flag_coercion(cls, data):
    for key, value in (data or {}).items():
        field = cls.model_fields.get(key)
        if field and value is not None and not isinstance(value, field.annotation):
            logger.warning("字段类型被强制转换", extra={"field": key, "value": repr(value)})
    return data
```

**根因**

宽松模式是"尽力而为"，它不区分"模型偶尔手抖"和"模型输出格式已经变了"。前者可以救，后者必须报警。

**修法**

关键路径切 `strict=True`；过渡期用上面的探针统计"强制转换率"，把它当一个**监控指标**看——它涨了，说明你的 prompt 或模型版本变了。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么归一化要写在 <code>mode="before"</code> 里？</b></summary>

因为 `after` 阶段 pydantic 已经做完类型转换，非法值（比如 `"紧急"` 对 `Priority`）早就抛错了，你拿不到原始输入去救。`before` 拿到的才是模型的原始值，此时才能把 `"紧急"` 映射成 `P0`。

</details>

<details>
<summary><b>Q2：为什么"版本号格式不对"比"缺字段"更值得当场拦住？</b></summary>

缺字段通常能靠默认值或下游兜底，而**格式错误的值会一路流到数据库、索引、下游接口**，在那里以"查不到""对不上"的形式爆出来，离根因极远。校验的意义就是**让错误停在你还能解释它的地方**。

</details>

<details>
<summary><b>Q3：<code>extra="forbid"</code> 和"宽容一点别报错"哪个对？</b></summary>

看有没有纠错回路。**有 `repair_message` 回喂机制时选 forbid**——多余字段会被立刻纠正，信息不丢，还顺手教会模型。**没有回路时选 ignore**——否则用户每次都会看到 500。所以这两个设计是绑定的，不能只挑一个。

</details>

### 9.2 动手任务：把「纠错回喂」闭环跑通

这是本节最有价值的作业，它把 1.3 和 1.4 接起来。

**要求**

实现：

```python
async def load_ticket_with_retry(
    client: RawLLMClient,
    messages: list[Message],
    *,
    max_rounds: int = 2,
) -> Ticket:
    ...
```

1. 第 1 轮：调 `client.chat(messages)`，用 `load_ticket()` 解析。
2. 解析失败：把 `exc.repair_message` 作为**新的 user 消息**追加进 `messages`，再调一次。
3. 最多 `max_rounds` 轮；仍失败则抛出最后一次的 `TicketParseError`。
4. 每一轮都打印：轮次、原始输出前 60 字、成败。

**验收标准**

- 用 1.3 的 mock 服务（`scripts.mock_llm_server`），第 1 次让它返回坏 JSON、第 2 次返回好 JSON，最终返回 `Ticket` 对象，且 `len(messages)` 增加了 3（原始 user + 坏 assistant + repair user）。
- 让 mock **每次都返回坏 JSON**：函数抛 `TicketParseError`，且总共只调用了 `max_rounds` 次（用 mock 的请求计数验证）。
- `max_rounds=1` 时只尝试一次，不重试。
- 补 3 个 pytest 用例覆盖上述三条。

**提示**：你需要给 mock 加一个"按调用次数返回不同内容"的能力。最简单的做法是给 mock 加一个 `mock_replies: list[str]` 参数，按顺序出队。这属于"测试基础设施"，值得花时间做——第 5 章的评估门禁会大量复用。

### 9.3 拓展挑战

给 `Ticket` 加一个嵌套模型：

```python
class Attachment(BaseModel):
    name: str
    size_bytes: int = Field(ge=0)
    url: str

class Ticket(BaseModel):
    attachments: list[Attachment] = Field(default_factory=list, max_length=3)
```

**思考**：

1. 嵌套校验失败时，`error["loc"]` 会变成 `("attachments", 0, "size_bytes")`，你的 `repair_message` 能正确显示成 `attachments.0.size_bytes` 吗？
2. `$defs` 里会多出几个定义？2.1 的 `inline_refs()` 需要处理几层嵌套？

---

## 10. 本节小结与延伸

**三点回顾**

1. **`json.loads` 只保证"是合法 JSON"，不保证"是你要的数据"**。pydantic 模型补上的是"结构 + 约束 + 跨字段规则"这三层。
2. **三层管线**：去代码块 → 括号配对抠取 → 校验。每一步都对应一类真实脏数据，缺哪步都会在线上炸。
3. **校验失败不是异常，是对话机会**。`repair_message` 要"精准且短"（实测 279 vs 612 字符），并且必须把 schema 表达不了的规则用自然语言再说一遍。

**已知限制（诚实记录）**

- `repair_message` 把"P0 必须审批"这条规则**硬编码在字符串里**。模型或规则一变，这里就会不同步。更稳的做法是从模型定义里自动生成规则描述——留到阶段五做评估时再回头处理。
- `$defs` 未展开，直接用于 OpenAI strict function calling 会失败。2.1 会实现 `inline_refs()`。

**延伸阅读**

- pydantic v2 模型与字段：https://docs.pydantic.dev/latest/concepts/models/
- pydantic v2 校验器（`field_validator` / `model_validator`）：https://docs.pydantic.dev/latest/concepts/validators/
- pydantic v2 迁移指南（v1 → v2 对照表）：https://docs.pydantic.dev/latest/migration/
- JSON Schema 规范：https://json-schema.org/understanding-json-schema/

---

## 修订记录

- 2026-11-17 初稿：8 个案例与 13 个测试全部实机运行，§7 为真实输出
- 2026-11-17 修正：`repair_message` 对 `missing` 类型不再打印整个对象（改为"（未提供）"）；去掉 pydantic 自动加的 `Value error, ` 前缀；demo 的成功/失败计数改为动态统计
- 2026-11-17 补充：§6.1 正则截断、§6.2 错误信息长度（612 vs 279）均为实测数据
