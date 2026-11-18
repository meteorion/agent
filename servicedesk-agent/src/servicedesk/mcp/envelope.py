# file: src/servicedesk/mcp/envelope.py
"""MCP 2026-07-28 的「无状态信封」。

这一版最根本的变化：**没有握手、没有 session**。每一个请求都必须自己说清楚
「我是谁、我说哪版协议、我支持什么能力」，服务端逐请求判断收或不收。

本模块只做四件很小的事，但每一件都是旧版不需要的：
1. 构造请求：把协议版本/身份/能力塞进 `_meta`
2. 构造 HTTP 头：`MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name`
3. 版本协商：不在支持列表里就抛 `UnsupportedProtocolVersionError`
4. 结果归一化：老 Server 没有 `resultType` 时按 `"complete"` 处理

⚠️ 字段名以官方 schema 为准（https://modelcontextprotocol.io/specification/2026-07-28/schema）。
本模块的 `_meta` 键取自规范正文示例；`server/discover` 的返回体字段名未在正文中逐一核实，
代码里做了容错读取，见 `supported_versions()`。
"""

from __future__ import annotations

import itertools
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

# 本节针对的协议版本
PROTOCOL_VERSION = "2026-07-28"
# 上一版（握手制）的版本号，用于演示旧客户端
LEGACY_PROTOCOL_VERSION = "2025-11-25"

# 所有 _meta 键都带这个前缀，避免和业务字段撞名
META_PREFIX = "io.modelcontextprotocol/"
META_PROTOCOL_VERSION = META_PREFIX + "protocolVersion"
META_CLIENT_INFO = META_PREFIX + "clientInfo"
META_CLIENT_CAPABILITIES = META_PREFIX + "clientCapabilities"
META_SERVER_INFO = META_PREFIX + "serverInfo"
META_LOG_LEVEL = META_PREFIX + "logLevel"

# 结果类型：普通结果 / 需要客户端补充输入（MRTR）
RESULT_COMPLETE = "complete"
RESULT_INPUT_REQUIRED = "input_required"
VALID_RESULT_TYPES = (RESULT_COMPLETE, RESULT_INPUT_REQUIRED)

_REQUEST_IDS = itertools.count(1)


class UnsupportedProtocolVersionError(RuntimeError):
    """服务端不支持请求里声明的协议版本。

    规范要求错误里**列出自己支持的版本**，否则客户端只能瞎猜或放弃。
    """

    def __init__(self, requested: str, supported: Iterable[str]) -> None:
        self.requested = requested
        self.supported = sorted(set(supported))
        super().__init__(
            f"不支持的协议版本 {requested!r}；服务端支持：{self.supported}"
        )


# ------------------------------- 构造请求 -------------------------------


def build_request(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    client_name: str = "servicedesk-agent",
    client_version: str = "0.1.0",
    client_capabilities: dict[str, Any] | None = None,
    protocol_version: str = PROTOCOL_VERSION,
    log_level: str | None = None,
    traceparent: str | None = None,
    request_id: int | None = None,
) -> dict[str, Any]:
    """构造一个自描述的 JSON-RPC 请求。

    `_meta` 放在 `params` 里（与规范示例一致），它替代了旧版 `initialize` 的全部作用。
    """
    meta: dict[str, Any] = {
        META_PROTOCOL_VERSION: protocol_version,
        META_CLIENT_INFO: {"name": client_name, "version": client_version},
    }
    if client_capabilities is not None:
        meta[META_CLIENT_CAPABILITIES] = client_capabilities
    if log_level is not None:
        # 旧版的 logging/setLevel 被移除了，日志级别改成每请求声明
        meta[META_LOG_LEVEL] = log_level
    if traceparent is not None:
        # W3C Trace Context 通过 _meta 传播，让链路能跨进程连起来
        meta["traceparent"] = traceparent

    body_params: dict[str, Any] = dict(params or {})
    body_params["_meta"] = meta

    return {
        "jsonrpc": "2.0",
        "id": request_id if request_id is not None else next(_REQUEST_IDS),
        "method": method,
        "params": body_params,
    }


def build_headers(
    method: str,
    *,
    name: str | None = None,
    protocol_version: str = PROTOCOL_VERSION,
    traceparent: str | None = None,
) -> dict[str, str]:
    """Streamable HTTP 必须带的头。

    `Mcp-Method` / `Mcp-Name` 让网关、限流器、WAF 不用解析 JSON 体就能路由与计量。
    """
    headers = {
        "Content-Type": "application/json",
        "MCP-Protocol-Version": protocol_version,
        "Mcp-Method": method,
    }
    if name:
        headers["Mcp-Name"] = name
    if traceparent:
        headers["traceparent"] = traceparent
    return headers


# ------------------------------- 版本协商 -------------------------------


def check_protocol_version(requested: str, supported: Iterable[str]) -> str:
    """逐请求判断版本。不匹配立刻抛错，不做"降级猜测"。"""
    supported_list = list(supported)
    if requested not in supported_list:
        raise UnsupportedProtocolVersionError(requested, supported_list)
    return requested


def supported_versions(discover_result: dict[str, Any]) -> list[str]:
    """从 `server/discover` 的返回里读出支持的版本列表。

    ⚠️ 返回体的确切字段名以 schema 为准，这里做了容错读取：
    依次尝试几个常见键，都取不到就返回空列表（而不是瞎猜一个版本）。
    """
    for key in ("protocolVersions", "supportedProtocolVersions", "versions"):
        value = discover_result.get(key)
        if isinstance(value, list) and value:
            return [str(item) for item in value]
    return []


# ----------------------------- 结果归一化 -----------------------------


def result_type(response: dict[str, Any]) -> str:
    """读取响应的 `resultType`；缺失或不认识时按 `"complete"` 处理。

    这是一条**向后兼容铁律**：老 Server（2025-11-25 及更早）不会带这个字段，
    如果把"缺失"当成错误，所有旧服务都会失效。
    """
    result = response.get("result")
    if not isinstance(result, dict):
        return RESULT_COMPLETE
    value = result.get("resultType")
    return value if value in VALID_RESULT_TYPES else RESULT_COMPLETE


def is_input_required(response: dict[str, Any]) -> bool:
    return result_type(response) == RESULT_INPUT_REQUIRED


def input_requests(response: dict[str, Any]) -> list[dict[str, Any]]:
    """MRTR：服务端说"我还需要这些信息"。"""
    result = response.get("result")
    if not isinstance(result, dict):
        return []
    requests = result.get("inputRequests")
    return list(requests) if isinstance(requests, list) else []


def build_retry_with_inputs(
    original_request: dict[str, Any],
    responses: dict[str, Any],
) -> dict[str, Any]:
    """MRTR 第二轮：**重试原请求**，把答案放进 `inputResponses`。

    注意是"重试原请求"而不是"发一个新请求"：方法、参数、`_meta` 全部保持不变，
    只多了 `inputResponses`。这样服务端不需要记住上一轮问过什么。
    """
    retried = {
        "jsonrpc": "2.0",
        "id": original_request["id"],
        "method": original_request["method"],
        "params": dict(original_request["params"]),
    }
    retried["params"]["inputResponses"] = responses
    return retried


# ---------------------------- 显式句柄（替代 session） ----------------------------


@dataclass
class HandleRecord:
    data: dict[str, Any]
    expires_at: float
    created_at: float = field(default_factory=time.time)


class HandleStore:
    """服务端签发的显式句柄，用来承载"跨调用状态"。

    旧版把状态藏在 transport 的 session 里，于是必须粘性路由、共享存储。
    新版让工具**签发一个 id**，由模型像普通参数一样带回来——状态回到业务层，
    实例本身保持无状态。
    """

    def __init__(self, ttl_s: float = 900.0) -> None:
        self._ttl_s = ttl_s
        self._records: dict[str, HandleRecord] = {}

    def mint(self, data: dict[str, Any]) -> str:
        handle = f"h_{uuid.uuid4().hex[:16]}"
        now = time.time()
        self._records[handle] = HandleRecord(
            data=dict(data), expires_at=now + self._ttl_s, created_at=now
        )
        return handle

    def resolve(self, handle: str) -> dict[str, Any] | None:
        record = self._records.get(handle)
        if record is None:
            return None
        if record.expires_at < time.time():
            self._records.pop(handle, None)
            return None
        return dict(record.data)

    def __len__(self) -> int:
        return len(self._records)
