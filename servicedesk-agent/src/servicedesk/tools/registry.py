# file: src/servicedesk/tools/registry.py
"""工具注册表：schema 生成 + 安全调用。

三个设计决定：
1. **schema 从 pydantic 模型自动生成**，改模型就等于改 schema，不会漏改
2. **调用失败返回字符串，不抛异常** —— 让模型有机会自我纠正
3. **参数先校验再执行** —— 绝不把脏参数传进业务函数
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from servicedesk.logging_setup import get_logger

log = get_logger("tools.registry")

Handler = Callable[..., Awaitable[Any]]


# ------------------------------- 数据结构 -------------------------------


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。content 永远是**字符串**，因为最终要回喂给模型。"""

    ok: bool
    content: str
    error: str | None = None
    duration_ms: float = 0.0


@dataclass
class ToolSpec:
    name: str
    description: str
    params_model: type[BaseModel]
    handler: Handler
    timeout_s: float = 5.0


@dataclass(frozen=True)
class InputRequest:
    """工具缺信息时，向调用方索取的一项内容（MRTR 用）。"""

    id: str
    prompt: str
    schema: dict[str, Any] = field(default_factory=dict)


class NeedsInput(Exception):
    """工具缺少必要信息，**不是失败**，需要调用方补一轮（MRTR）。

    注意它和 `ToolResult(ok=False)` 的区别：
        NeedsInput      → 协议层返回 `resultType: "input_required"`
        ok=False        → 协议层返回成功响应，但 `isError: true`
    """

    def __init__(self, requests: list[InputRequest]) -> None:
        self.requests = requests
        super().__init__(f"需要补充 {len(requests)} 项信息：{[r.id for r in requests]}")


# ------------------------------- schema 工具 -------------------------------


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """把 JSON Schema 里的 $ref/$defs 递归展开成内联结构。

    为什么必须做：OpenAI 的 strict function calling **不接受 $ref**。
    pydantic 会把枚举、嵌套模型都抽到 $defs 里，直接用会被服务端拒绝。
    （1.4 的 §5.6 埋了这个伏笔，这里兑现。）
    """
    definitions = schema.get("$defs", {})

    def resolve(node: Any, depth: int = 0) -> Any:
        if depth > 20:                      # 防循环引用打爆栈
            return {}
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                name = ref.rsplit("/", 1)[-1]
                target = definitions.get(name, {})
                merged = {k: v for k, v in node.items() if k != "$ref"}
                return {**resolve(target, depth + 1), **resolve(merged, depth + 1)}
            return {
                key: resolve(value, depth + 1)
                for key, value in node.items()
                if key != "$defs"
            }
        if isinstance(node, list):
            return [resolve(item, depth + 1) for item in node]
        return node

    resolved = resolve(schema)
    if isinstance(resolved, dict):
        resolved.pop("$defs", None)
    return resolved if isinstance(resolved, dict) else {}


def tool_schema(name: str, description: str, params_model: type[BaseModel]) -> dict[str, Any]:
    """生成 OpenAI 风格的单个工具定义。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": inline_refs(params_model.model_json_schema()),
        },
    }


# -------------------------------- 注册表 --------------------------------


class ToolRegistry:
    """线程内共享的工具集合。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        params_model: type[BaseModel],
        timeout_s: float = 5.0,
    ) -> Callable[[Handler], Handler]:
        """装饰器写法：

            @registry.register(name="query_asset", description="…", params_model=QueryAssetParams)
            async def query_asset(hostname: str) -> dict: ...
        """

        def decorator(handler: Handler) -> Handler:
            if name in self._tools:
                raise ValueError(f"工具名重复：{name}")
            self._tools[name] = ToolSpec(
                name=name,
                description=description,
                params_model=params_model,
                handler=handler,
                timeout_s=timeout_s,
            )
            return handler

        return decorator

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """注意：**顺序稳定**。顺序乱跳会破坏 prompt cache（2.5 会讲）。"""
        return [
            tool_schema(spec.name, spec.description, spec.params_model)
            for spec in (self._tools[name] for name in self.names())
        ]

    # -- 执行 --

    async def call(self, name: str, arguments: str) -> ToolResult:
        """执行一次工具调用。**任何失败都以字符串返回，不抛异常。**"""
        started = time.perf_counter()

        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                ok=False,
                content=f"错误：没有名为 {name!r} 的工具。可用工具：{self.names()}",
                error="unknown_tool",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        # 1) 参数是字符串，先解析
        try:
            raw_args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return ToolResult(
                ok=False,
                content=f"错误：参数不是合法 JSON（{exc.msg}）。请重新生成参数。",
                error="bad_json",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        if not isinstance(raw_args, dict):
            return ToolResult(
                ok=False,
                content="错误：参数必须是一个 JSON 对象。",
                error="bad_json_type",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        # 2) 参数校验（把模型的输出当成不可信输入）
        try:
            params = spec.params_model.model_validate(raw_args)
        except ValidationError as exc:
            problems = []
            for error in exc.errors():
                location = ".".join(str(part) for part in error.get("loc") or ()) or "整体"
                problems.append(f"{location}：{error.get('msg')}")
            return ToolResult(
                ok=False,
                content="错误：参数校验失败 —— " + "；".join(problems),
                error="invalid_params",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        # 3) 执行（带超时，别让一个慢工具拖死整轮对话）
        try:
            payload = await asyncio.wait_for(
                spec.handler(**params.model_dump()), timeout=spec.timeout_s
            )
        except TimeoutError:
            log.warning("tool_timeout", tool=name, timeout_s=spec.timeout_s)
            return ToolResult(
                ok=False,
                content=f"错误：工具 {name} 执行超过 {spec.timeout_s}s，已中止。",
                error="timeout",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except NeedsInput:
            # 缺信息不是失败，原样抛给协议层去转成 input_required
            raise
        except Exception as exc:  # noqa: BLE001 —— 工具内部错误不该炸掉对话
            log.warning("tool_failed", tool=name, error=repr(exc))
            return ToolResult(
                ok=False,
                content=f"错误：工具 {name} 执行失败（{type(exc).__name__}: {exc}）。",
                error="exception",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        duration_ms = (time.perf_counter() - started) * 1000
        log.info(
            "tool_called",
            tool=name,
            ok=True,
            duration_ms=round(duration_ms, 1),
            args_size=len(arguments),
        )
        return ToolResult(
            ok=True,
            content=json.dumps(payload, ensure_ascii=False),
            duration_ms=duration_ms,
        )
