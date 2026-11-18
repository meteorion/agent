# file: src/servicedesk/graph/tools_bridge.py
"""把 MCP 工具接进 Agent。

两条路，本文件都实现：

1. **真实适配器**：`langchain_mcp_adapters` 把远端 MCP 工具变成 LangChain `BaseTool`
2. **接回我们的循环**：把 `BaseTool` 包成 2.1 的 `ToolRegistry`，让 `run_tool_loop` 直接用

顺带解决三件生产上必须处理的事：
- **命名空间**：多个 Server 可能有同名工具 → `mcp__demo__query_asset`
- **白名单**：只暴露当前任务需要的工具（工具越多，选错的概率越高）
- **token 预算**：工具 schema 会占 prompt，必须能算、能砍
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from servicedesk.logging_setup import get_logger
from servicedesk.tools.registry import ToolRegistry

log = get_logger("graph.bridge")

MCP_TOOL_SEP = "__"


# ------------------------------- 命名空间 -------------------------------


def namespaced(server: str, tool: str) -> str:
    """`demo` + `query_asset` → `mcp__demo__query_asset`。

    为什么必须加前缀：两个 MCP Server 都暴露 `search` 时，裸名会互相覆盖——
    而且是**后注册的悄悄赢**，没有任何报错。
    """
    return f"mcp{MCP_TOOL_SEP}{server}{MCP_TOOL_SEP}{tool}"


def split_namespaced(name: str) -> tuple[str | None, str]:
    parts = name.split(MCP_TOOL_SEP)
    if len(parts) >= 3 and parts[0] == "mcp":
        return parts[1], MCP_TOOL_SEP.join(parts[2:])
    return None, name


# -------------------------------- 描述信息 --------------------------------


@dataclass(frozen=True)
class McpToolInfo:
    name: str                 # 命名空间后的名字
    original: str             # 远端原名
    server: str
    description: str
    input_schema: dict[str, Any]


def estimate_schema_tokens(infos: Iterable[McpToolInfo]) -> int:
    """工具 schema 占多少 prompt token（粗估：字符 / 1.5）。"""
    payload = json.dumps(
        [{"name": i.name, "description": i.description, "parameters": i.input_schema} for i in infos],
        ensure_ascii=False,
    )
    return max(1, round(len(payload) / 1.5))


# -------------------------------- 桥接器 --------------------------------


class McpToolBridge:
    """把一组 LangChain `BaseTool`（来自 MCP）适配成项目内可用的形态。"""

    def __init__(
        self,
        tools: list[Any],
        *,
        server: str,
        include: Iterable[str] | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self.server = server
        self.timeout_s = timeout_s
        self._tools: dict[str, Any] = {}
        self._infos: dict[str, McpToolInfo] = {}

        wanted = set(include) if include is not None else None
        for tool in tools:
            original = getattr(tool, "name", "")
            if not original:
                continue
            if wanted is not None and original not in wanted:
                continue
            full = namespaced(server, original)
            if full in self._tools:
                raise ValueError(f"工具名冲突：{full}")
            self._tools[full] = tool
            self._infos[full] = McpToolInfo(
                name=full,
                original=original,
                server=server,
                description=str(getattr(tool, "description", "") or ""),
                input_schema=_schema_of(tool),
            )

        if wanted is not None:
            missing = wanted - {info.original for info in self._infos.values()}
            if missing:
                raise ValueError(f"白名单里有不存在的工具：{sorted(missing)}")

    # -- 查询 --

    def infos(self) -> list[McpToolInfo]:
        return [self._infos[name] for name in sorted(self._infos)]

    def schema_tokens(self) -> int:
        return estimate_schema_tokens(self.infos())

    def names(self) -> list[str]:
        return sorted(self._tools)

    # -- 转成项目内的 ToolRegistry --

    def to_registry(self) -> ToolRegistry:
        """让 2.1 的 `run_tool_loop` 能直接调用远端 MCP 工具。"""
        registry = ToolRegistry()
        for full_name in self.names():
            tool = self._tools[full_name]
            info = self._infos[full_name]
            params_model = _params_model(tool)

            async def proxy(_tool=tool, **_kwargs: Any) -> Any:
                result = await _tool.ainvoke(_kwargs)
                return _normalize(result)

            if params_model is None:
                log.warning("tool_without_args_schema", tool=full_name)
                continue

            registry.register(
                name=full_name,
                description=info.description,
                params_model=params_model,
                timeout_s=self.timeout_s,
            )(proxy)
        return registry


def _params_model(tool: Any) -> Any:
    """取得工具参数对应的 pydantic 模型。

    注意：`args_schema` 在 langchain-mcp-adapters 里是**JSON Schema 字典**，
    不是模型类；真正的模型要用 `get_input_schema()` 拿。
    """
    getter = getattr(tool, "get_input_schema", None)
    if callable(getter):
        model = getter()
        if hasattr(model, "model_validate"):
            return model
    raw = getattr(tool, "args_schema", None)
    if isinstance(raw, type) and hasattr(raw, "model_validate"):
        return raw
    return None


def _schema_of(tool: Any) -> dict[str, Any]:
    model = _params_model(tool)
    if model is not None:
        return model.model_json_schema()
    raw = getattr(tool, "args_schema", None)
    if isinstance(raw, dict):
        return raw
    return {"type": "object", "properties": {}}


def _normalize(result: Any) -> Any:
    """MCP 工具可能返回 str / dict / list[content-block]，统一成可 JSON 化的形态。

    注意 list 的典型形状是 `[{"type":"text","text":"..."}]` —— 直接把整块
    丢给模型会得到 `{"items":[{"type":"text",...}]}` 这种噪声，所以先抽 text。
    """
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"content": result}
    if isinstance(result, list):
        texts = [
            item.get("text")
            for item in result
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if texts:
            joined = "\n".join(str(text) for text in texts)
            try:
                return json.loads(joined)
            except json.JSONDecodeError:
                return {"content": joined}
        return {"items": result}
    return result


# ------------------------------ 懒加载目录 ------------------------------


@dataclass
class ToolCatalog:
    """先看清单、再决定加载哪些。

    `discover()` 只拿元信息（name/description/schema），不产生调用；
    `load(names)` 才真正把工具装进 registry。

    为什么要分两步：工具一多，schema 本身就吃掉几千 token。
    **先看目录，再按需加载**，是控制上下文成本的第一道闸。
    """

    bridge: McpToolBridge

    def discover(self) -> list[McpToolInfo]:
        return self.bridge.infos()

    def load(self, names: Iterable[str]) -> ToolRegistry:
        wanted = set(names)
        available = {info.original for info in self.bridge.infos()}
        unknown = wanted - available
        if unknown:
            raise ValueError(f"要加载的工具不存在：{sorted(unknown)}")
        return McpToolBridge(
            list(self.bridge._tools.values()),      # noqa: SLF001 —— 内部复用
            server=self.bridge.server,
            include=wanted,
            timeout_s=self.bridge.timeout_s,
        ).to_registry()
