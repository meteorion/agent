# file: src/servicedesk/mcp/server.py
"""一个无状态 MCP Server：server/discover + Tools / Resources / Prompts。

不依赖任何 MCP SDK —— 手写 JSON-RPC 才能看清协议。三个设计要点：

1. **没有 initialize、没有 session**：每个请求自带 `_meta`，逐请求判断版本
2. **stdout 是协议通道**：日志只能写 stderr，否则客户端解析会崩
3. **tools/list 必须确定性排序 + 带 ttlMs**：客户端才能缓存，prompt cache 才稳定

⚠️ 各原语的返回体字段名按规范 schema 的常规形状实现；
`server/discover` 的返回体字段名未逐字核实，见 `docs/mcp-stateless.md` 的说明。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

from servicedesk.mcp.envelope import (
    META_LOG_LEVEL,
    META_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
)
from servicedesk.tools.builtin import build_default_registry
from servicedesk.tools.registry import NeedsInput, ToolRegistry

SERVER_NAME = "servicedesk-mcp"
SERVER_VERSION = "0.1.0"

# 支持协商的版本。过渡期应该是 dual-era（见 2.3 §8 坑 1），这里只留新版，突出"逐请求判断"
SUPPORTED_VERSIONS = [PROTOCOL_VERSION]
# 过渡期：同时接受旧版握手（dual-era）。2.3 §8 坑 1 的建议
LEGACY_VERSION = "2025-11-25"

# 日志级别强弱关系（数值越大越严重）
LOG_LEVELS = {"debug": 10, "info": 20, "notice": 25, "warning": 30, "error": 40}
DEFAULT_LOG_LEVEL = "info"

# JSON-RPC 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ------------------------------- 静态内容 -------------------------------


@dataclass(frozen=True)
class Resource:
    uri: str
    name: str
    mime_type: str
    description: str
    text: str


@dataclass(frozen=True)
class Prompt:
    name: str
    description: str
    arguments: list[dict[str, Any]]
    template: str


RESOURCES: list[Resource] = [
    Resource(
        uri="doc://vpn-guide",
        name="VPN 使用与排障手册",
        mime_type="text/markdown",
        description="公司 VPN 的安装、连接与常见故障处理（只读）",
        text=(
            "# VPN 使用与排障手册\n\n"
            "## 4.1 提示「认证失败」\n"
            "通常有三类原因：密码过期、动态口令时间偏差、账号被锁定。\n\n"
            "## 4.2 提示「无法建立隧道」\n"
            "通常是本地网络策略或代理导致，与账号无关。\n"
        ),
    ),
]

PROMPTS: list[Prompt] = [
    Prompt(
        name="explain_ticket",
        description="把一个工单解释成给用户看的处理建议",
        arguments=[
            {
                "name": "ticket_id",
                "description": "工单编号，形如 T-1001",
                "required": True,
            }
        ],
        template=(
            "请用通俗的语言解释工单 {ticket_id} 的处理进度，"
            "并给出员工下一步该做什么。不要编造工单里没有的信息。"
        ),
    ),
]


# ------------------------------- 服务器 -------------------------------


@dataclass
class McpServer:
    """纯逻辑服务器：`handle()` 收一个 dict，回一个 dict（或 None 表示通知）。"""

    registry: ToolRegistry = field(default_factory=build_default_registry)
    resources: list[Resource] = field(default_factory=lambda: list(RESOURCES))
    prompts: list[Prompt] = field(default_factory=lambda: list(PROMPTS))
    log_stream: TextIO = field(default_factory=lambda: sys.stderr)

    # -- 日志（只写 stderr） --

    def _log(self, level: str, event: str, **fields: Any) -> None:
        line = f"[{level}] {event}"
        if fields:
            line += " " + " ".join(f"{k}={v}" for k, v in fields.items())
        print(line, file=self.log_stream, flush=True)

    def log_level_of(self, params: dict[str, Any]) -> str:
        """日志级别来自**每个请求**的 `_meta`，不是连接级设置。

        旧版的 `logging/setLevel` 已被移除。
        """
        meta = params.get("_meta") or {}
        value = str(meta.get(META_LOG_LEVEL) or DEFAULT_LOG_LEVEL).lower()
        return value if value in LOG_LEVELS else DEFAULT_LOG_LEVEL

    def _log_at(self, request_level: str, level: str, event: str, **fields: Any) -> None:
        if LOG_LEVELS[level] >= LOG_LEVELS[request_level]:
            self._log(level, event, **fields)

    # -- 入口 --

    def _precheck(
        self, request: dict[str, Any]
    ) -> tuple[Any, str, dict[str, Any], str] | dict[str, Any]:
        """公共前置校验。返回四元组表示通过，返回 dict 表示已是错误响应。"""
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if not isinstance(method, str) or not method:
            return self._error(request_id, INVALID_REQUEST, "缺少 method")
        if not isinstance(params, dict):
            return self._error(request_id, INVALID_PARAMS, "params 必须是对象")

        request_level = self.log_level_of(params)
        self._log_at(request_level, "debug", "request_received", method=method, id=request_id)

        # 版本逐请求判断：不在支持列表里就拒绝（不做降级猜测）
        declared = (params.get("_meta") or {}).get(META_PROTOCOL_VERSION)
        if declared == LEGACY_VERSION:
            declared = None          # legacy 声明在握手里，不在 _meta
        if declared is not None and declared not in SUPPORTED_VERSIONS:
            return self._error(
                request_id,
                INVALID_REQUEST,
                f"不支持的协议版本 {declared!r}",
                {"supported": SUPPORTED_VERSIONS},
            )

        return request_id, method, params, request_level

    @staticmethod
    def _is_legacy(request: dict[str, Any]) -> bool:
        """双栈：`initialize` 请求与旧版声明都按 legacy 形状响应。"""
        params = request.get("params") or {}
        declared = (params.get("_meta") or {}).get(META_PROTOCOL_VERSION)
        return request.get("method") == "initialize" or declared == LEGACY_VERSION

    async def handle_async(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """唯一对外入口。`tools/call` 需要 await，所以整体是协程。"""
        checked = self._precheck(request)
        if isinstance(checked, dict):
            return checked
        request_id, method, params, request_level = checked

        try:
            if method == "tools/call":
                return await self._call_tool(request_id, params, request_level)
            result = self._dispatch(method, params)
        except MethodNotFound as exc:
            return self._error(request_id, METHOD_NOT_FOUND, str(exc))
        except JsonRpcInvalidParams as exc:
            return self._error(request_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 —— 一个异常不能打崩整个循环
            self._log("error", "internal_error", method=method, error=repr(exc))
            return self._error(request_id, INTERNAL_ERROR, f"内部错误：{type(exc).__name__}")

        self._log_at(request_level, "debug", "request_done", method=method)

        if self._is_legacy(request):
            # 旧客户端不认识这些新字段，给它干净的旧形状
            legacy_result = {k: v for k, v in result.items() if k not in ("ttlMs", "cacheScope")}
            return {"jsonrpc": "2.0", "id": request_id, "result": legacy_result}

        # 每个结果都必须带 resultType（2.3 §2.4）
        return {"jsonrpc": "2.0", "id": request_id, "result": {"resultType": "complete", **result}}

    async def _call_tool(
        self, request_id: Any, params: dict[str, Any], request_level: str
    ) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            raise JsonRpcInvalidParams("tools/call 需要 name")

        # MRTR 重试：把上一轮索要的答案并进参数。
        # 注意服务端**不记得**上一轮问过什么——答案就在这个请求里（这就是无状态）。
        responses = params.get("inputResponses") or {}
        merged = {**arguments, **{k: v for k, v in responses.items() if v is not None}}

        try:
            outcome = await self.registry.call(name, json.dumps(merged, ensure_ascii=False))
        except NeedsInput as exc:
            self._log_at(
                request_level, "info", "tool_needs_input", tool=name, fields=[r.id for r in exc.requests]
            )
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": "input_required",
                    "inputRequests": [
                        {"id": item.id, "prompt": item.prompt, "schema": item.schema}
                        for item in exc.requests
                    ],
                },
            }

        self._log_at(
            request_level,
            "info" if outcome.ok else "warning",
            "tool_called",
            tool=name,
            ok=outcome.ok,
            duration_ms=round(outcome.duration_ms, 1),
        )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "resultType": "complete",
                "content": [{"type": "text", "text": outcome.content}],
                "isError": not outcome.ok,
            },
        }

    # -- 分发（同步方法） --

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            # dual-era：旧客户端仍会先握手。这里不做任何状态记录。
            return {
                "protocolVersion": LEGACY_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method == "server/discover":
            return self.discover()
        if method == "tools/list":
            return self.list_tools()
        if method == "resources/list":
            return self.list_resources()
        if method == "resources/read":
            return self.read_resource(params)
        if method == "prompts/list":
            return self.list_prompts()
        if method == "prompts/get":
            return self.get_prompt(params)
        raise MethodNotFound(f"未知方法 {method!r}")

    # -- 三个原语 --

    def discover(self) -> dict[str, Any]:
        """Server **必须**实现：广播支持的版本、能力与身份。"""
        return {
            "protocolVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def list_tools(self) -> dict[str, Any]:
        """确定性排序 + 缓存提示。顺序一变，prompt cache 就失效。"""
        tools = [
            {
                "name": schema["function"]["name"],
                "description": schema["function"]["description"],
                "inputSchema": schema["function"]["parameters"],
            }
            for schema in self.registry.schemas()
        ]
        tools.sort(key=lambda item: item["name"])
        return {
            "tools": tools,
            "ttlMs": 60_000,
            "cacheScope": "global",
        }

    def list_resources(self) -> dict[str, Any]:
        items = [
            {
                "uri": resource.uri,
                "name": resource.name,
                "description": resource.description,
                "mimeType": resource.mime_type,
            }
            for resource in sorted(self.resources, key=lambda r: r.uri)
        ]
        return {"resources": items, "ttlMs": 60_000, "cacheScope": "global"}

    def read_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise JsonRpcInvalidParams("resources/read 需要 uri")
        for resource in self.resources:
            if resource.uri == uri:
                return {
                    "contents": [
                        {
                            "uri": resource.uri,
                            "mimeType": resource.mime_type,
                            "text": resource.text,
                        }
                    ]
                }
        raise JsonRpcInvalidParams(f"未知资源 {uri!r}")

    def list_prompts(self) -> dict[str, Any]:
        items = [
            {
                "name": prompt.name,
                "description": prompt.description,
                "arguments": prompt.arguments,
            }
            for prompt in sorted(self.prompts, key=lambda p: p.name)
        ]
        return {"prompts": items, "ttlMs": 60_000, "cacheScope": "global"}

    def get_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise JsonRpcInvalidParams("prompts/get 需要 name")
        arguments = params.get("arguments") or {}

        for prompt in self.prompts:
            if prompt.name != name:
                continue
            missing = [
                arg["name"]
                for arg in prompt.arguments
                if arg.get("required") and not arguments.get(arg["name"])
            ]
            if missing:
                raise JsonRpcInvalidParams(f"缺少参数：{missing}")
            text = prompt.template.format(**arguments)
            return {
                "description": prompt.description,
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
            }
        raise JsonRpcInvalidParams(f"未知提示模板 {name!r}")

    # -- 响应构造 --

    @staticmethod
    def _error(
        request_id: Any,
        code: int,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


class MethodNotFound(Exception):
    pass


class JsonRpcInvalidParams(Exception):
    pass


# ------------------------------- stdio 传输 -------------------------------


async def serve_stdio(
    server: McpServer | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """按行读 JSON-RPC，按行写响应。

    **stdout 只能写协议消息**——往里打一行日志，客户端解析就会崩。
    """
    server = server or McpServer()
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response: dict[str, Any] | None = McpServer._error(
                None, PARSE_ERROR, f"JSON 解析失败：{exc.msg}"
            )
        else:
            response = await server.handle_async(request)

        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> int:
    import asyncio

    from servicedesk.logging_setup import configure_logging

    # stdout 是协议通道，日志必须走 stderr
    configure_logging(level="INFO", stream=sys.stderr)
    asyncio.run(serve_stdio())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
