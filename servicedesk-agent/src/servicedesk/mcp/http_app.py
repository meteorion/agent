# file: src/servicedesk/mcp/http_app.py
"""把 2.4 的 Server 挂到 Streamable HTTP 上，并支持 subscriptions/listen。

四个关键点：
1. **单端点 POST**：所有 JSON-RPC 都走 `POST /mcp`，不再有独立 GET 端点
2. **路由头**：`Mcp-Method` / `Mcp-Name` 必须与 body 一致，否则网关能被骗
3. **订阅用一条长连接 POST 响应流**：按类型 opt-in，事件打 `subscriptionId`
4. **断了就是丢了**：没有 Last-Event-ID 续传，客户端必须用**新 request id 重发**

无状态的含义在这里最直观：**每个请求各自 `asyncio.run` 一个事件循环**，
实例本身不持有任何会话。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from servicedesk.logging_setup import configure_logging, get_logger
from servicedesk.mcp.auth import (
    AuthError,
    InsufficientScope,
    TokenStore,
    check_scope,
    parse_bearer,
)
from servicedesk.obs.tracing import Span, Tracer, exporter_path, extract_or_new
from servicedesk.mcp.envelope import (
    META_PREFIX,
    PROTOCOL_VERSION,
    HandleStore,
    result_type,
)
from servicedesk.mcp.server import McpServer

log = get_logger("mcp.http")

ENDPOINT = "/mcp"
META_SUBSCRIPTION_ID = META_PREFIX + "subscriptionId"

# 可订阅的变更类型（客户端按类型 opt-in）
SUBSCRIPTION_TYPES = {
    "toolsListChanged",
    "promptsListChanged",
    "resourcesListChanged",
    "resourceSubscriptions",
}

# 订阅流最多推几条就收尾（生产里是长连接，这里为了让 demo/测试能结束）
MAX_STREAM_EVENTS = 6


class McpHTTPHandler(BaseHTTPRequestHandler):
    server_version = "ServicedeskMCP/0.1"

    # 由 create_server 注入
    mcp: McpServer
    handles: HandleStore
    tokens: TokenStore | None = None      # None = 开发模式，不校验鉴权
    tracer: Tracer | None = None          # 设了就记录 span（5.5）

    def log_message(self, *args: Any) -> None:
        pass  # 访问日志走 structlog，别打两份

    # ------------------------------ 入口 ------------------------------

    def do_POST(self) -> None:  # noqa: N802
        # 5.5：入口处取 traceparent；有就沿用，没有就新建
        self._trace_ctx = extract_or_new(dict(self.headers)).child()
        self._trace_started = time.time()
        if self.path.rstrip("/") != ENDPOINT:
            self._json(404, {"error": {"message": f"未知路径 {self.path}"}})
            return

        method_header = self.headers.get("Mcp-Method")
        name_header = self.headers.get("Mcp-Name")
        version_header = self.headers.get("MCP-Protocol-Version")

        # 鉴权在解析 body 之前做：401 不该浪费一次 JSON 解析
        claims = None
        if self.tokens is not None:
            try:
                claims = self.tokens.verify(parse_bearer(self.headers.get("Authorization")))
            except AuthError as exc:
                # 先把请求体排空：否则客户端还在发 body，连接会被 RST
                self._drain_body()
                self._json(
                    exc.status,
                    {"error": {"code": exc.error_code, "message": str(exc)}},
                    extra_headers={"WWW-Authenticate": exc.www_authenticate},
                )
                return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self._body_drained = True
        try:
            request = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            self._json(
                400,
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": f"JSON 解析失败：{exc.msg}"}},
            )
            return

        body_method = request.get("method")
        is_notification = request.get("id") is None

        # 哪些请求可以不带路由头？只有真正的 legacy 流量：
        #   - initialize 握手
        #   - JSON-RPC 通知（没有 id）
        #   - 已声明 2025-11-25 的后续请求
        legacy_ok = (
            body_method == "initialize"
            or is_notification
            or version_header == "2025-11-25"
        )
        if not method_header:
            if not legacy_ok:
                self._json(400, {"error": {"message": "缺少 Mcp-Method 头"}})
                return
            method_header = body_method       # 补上，后续逻辑统一

        # 头与 body 必须一致，否则可以用一个"合法"的头把请求偷运到别的处理器
        if body_method != method_header:
            self._json(
                400,
                {"error": {"message": f"Mcp-Method 头({method_header}) 与请求体方法({body_method}) 不一致"}},
            )
            return
        if name_header and isinstance(request.get("params"), dict):
            if request["params"].get("name") not in (None, name_header):
                self._json(400, {"error": {"message": "Mcp-Name 与参数不一致"}})
                return

        # 双栈：过渡期同时接受旧版版本头
        if version_header and version_header not in (PROTOCOL_VERSION, "2025-11-25"):
            self._json(
                400,
                {"jsonrpc": "2.0", "id": request.get("id"),
                 "error": {"code": -32600, "message": f"不支持的协议版本 {version_header!r}",
                           "data": {"supported": [PROTOCOL_VERSION]}}},
            )
            return

        # 权限检查：scope 不够是 403，不是 401
        if claims is not None and body_method == "tools/call":
            call_params = request.get("params") or {}
            tool_name = str(call_params.get("name") or "")
            try:
                check_scope(claims, tool_name, call_params.get("arguments"))
            except InsufficientScope as exc:
                log.warning("insufficient_scope", tool=tool_name, subject=claims.subject)
                self._json(
                    exc.status,
                    {"error": exc.payload},
                    extra_headers={"WWW-Authenticate": exc.www_authenticate},
                )
                return

        if is_notification:
            # JSON-RPC 通知不需要响应体：跑完就回 202，避免客户端解析空响应
            asyncio.run(self.mcp.handle_async(request))
            self._status_only(202)
            return

        if body_method == "subscriptions/listen":
            self._stream_subscriptions(request)
            return

        # 无状态：每个请求独立跑一个事件循环，实例不保留任何上下文
        response = asyncio.run(self.mcp.handle_async(request))

        # 句柄：如果工具返回里带了 handle 参数，说明有跨调用状态
        if response is not None and isinstance(request.get("params"), dict):
            params = request["params"]
            if isinstance(params.get("arguments"), dict) and "handle" in params["arguments"]:
                handle = params["arguments"]["handle"]
                state = self.handles.resolve(str(handle))
                log.info("handle_resolved", handle=handle, found=state is not None)

        if response is not None:
            self._json(200, response)

    # ---------------------------- 订阅流 ----------------------------

    def _stream_subscriptions(self, request: dict[str, Any]) -> None:
        params = request.get("params") or {}
        requested = params.get("types") or ["toolsListChanged"]
        accepted = [item for item in requested if item in SUBSCRIPTION_TYPES]
        rejected = [item for item in requested if item not in SUBSCRIPTION_TYPES]

        subscription_id = f"sub_{uuid.uuid4().hex[:12]}"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        # 第一条事件就是这条 listen 请求的响应（带上 subscriptionId）
        self._sse(
            {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "resultType": "complete",
                    "subscriptionId": subscription_id,
                    "types": accepted,
                    "rejected": rejected,
                },
            }
        )

        # 之后按类型推送变更通知，每条都打 subscriptionId
        emitted = 0
        for item in accepted:
            for seq in range(2):
                if emitted >= MAX_STREAM_EVENTS:
                    break
                self._sse(
                    {
                        "jsonrpc": "2.0",
                        "method": f"notifications/{item}",
                        "params": {
                            "_meta": {META_SUBSCRIPTION_ID: subscription_id},
                            "seq": seq,
                            "at": time.time(),
                        },
                    }
                )
                emitted += 1
                time.sleep(0.01)

        log.info(
            "subscription_stream_closed",
            subscription_id=subscription_id,
            accepted=accepted,
            rejected=rejected,
            events=emitted,
        )

    # ---------------------------- 工具方法 ----------------------------

    def _status_only(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _drain_body(self) -> None:
        """把请求体读完（幂等）。已经读过或没有 body 就什么都不做。"""
        if getattr(self, "_body_drained", False):
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            try:
                self.rfile.read(length)
            except OSError:
                pass
        self._body_drained = True

    def _sse(self, payload: dict[str, Any]) -> None:
        try:
            self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        except ConnectionError:
            # 客户端提前断开：这正是"流断了 = 请求丢了"的时刻
            log.warning("subscription_stream_aborted")

    def _json(
        self, status: int, body: dict[str, Any], *, extra_headers: dict[str, str] | None = None
    ) -> None:
        # 5.5：所有 JSON 响应都从这里出去，所以 span 在这里收口
        ctx = getattr(self, "_trace_ctx", None)
        if ctx is not None and self.tracer is not None:
            span = Span(
                name="mcp.handle",
                ctx=ctx,
                started_at=getattr(self, "_trace_started", time.time()),
                ended_at=time.time(),
                attributes={"status": status, "path": self.path},
            )
            self.tracer._finish(span)  # noqa: SLF001 —— demo 用的极简 tracer
        # 统一在这里排空：任何提前返回的响应都不该让客户端"发着 body 被关连接"
        self._drain_body()
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except ConnectionError:
            return


# ------------------------------ 装配与启动 ------------------------------


def make_handler(
    mcp: McpServer, handles: HandleStore, tokens: TokenStore | None, tracer: Tracer | None = None
) -> type[McpHTTPHandler]:
    class Bound(McpHTTPHandler):
        pass

    Bound.mcp = mcp
    Bound.handles = handles
    Bound.tokens = tokens
    Bound.tracer = tracer
    return Bound


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    handles: HandleStore | None = None,
    tokens: TokenStore | None = None,
    trace_log: str | None = None,
) -> tuple[ThreadingHTTPServer, str]:
    """起一个实例。port=0 表示随机端口。

    `tokens=None` 表示开发模式（不校验鉴权）。**生产必须传 TokenStore。**
    """
    mcp = McpServer(log_stream=sys.stderr)
    bound_handles = handles or HandleStore()
    tracer = Tracer(exporter=exporter_path() if trace_log is None else Path(trace_log))
    handler = make_handler(mcp, bound_handles, tokens, tracer)
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real_port = server.server_address[1]
    return server, f"http://{host}:{real_port}{ENDPOINT}"


def main() -> int:
    import os

    configure_logging(level="INFO", stream=sys.stderr)
    port = int(os.environ.get("PORT", "8080"))
    server, url = create_server(port=port)
    print(f"MCP Streamable HTTP 已启动: {url}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
