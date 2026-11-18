# file: scripts/trace_service.py
"""5.5 的子进程：一个会透传 traceparent 的 HTTP 服务。

用法：
    TRACE_LOG=<file> DOWNSTREAM=<url> uv run python -m scripts.trace_service <port>

行为：
    1. 从请求头取 traceparent（没有就新建）
    2. 开一个自己的 span（parent = 上游的 span_id）
    3. 如果有 DOWNSTREAM，**用自己作为父**再往下游发一次
    4. 响应里回自己的 trace_id / span_id，便于断言
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from servicedesk.obs.tracing import (
    Tracer,
    extract_or_new,
    format_traceparent,
    inject,
)


def build_handler(tracer: Tracer, downstream: str | None, name: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "TraceService/0.1"

        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length) if length else None

            incoming = extract_or_new(dict(self.headers))
            mine = incoming.child()

            with tracer.span(f"{name}.handle", mine) as span:
                span.attributes["path"] = self.path
                downstream_trace: dict[str, str] = {}
                if downstream:
                    # 关键：发给下游的 header 里放**我自己**的 span_id，
                    # 这样下游的 parent 才指向这一跳。
                    headers = {"Content-Type": "application/json"}
                    inject(headers, mine)
                    span.events.append("downstream_call")
                    try:
                        with httpx.Client(timeout=10) as client:
                            response = client.post(f"{downstream}/call", json={"from": name},
                                                   headers=headers)
                        span.attributes["downstream_status"] = response.status_code
                        downstream_trace = response.json()
                    except Exception as exc:  # noqa: BLE001
                        span.error = f"{type(exc).__name__}: {exc}"
                        downstream_trace = {"error": str(exc)}

            payload = {
                "service": name,
                "trace_id": mine.trace_id,
                "span_id": mine.span_id,
                "parent_id": mine.parent_id,
                "downstream": downstream_trace,
            }
            self._json(200, payload)

        def _json(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except ConnectionError:
                return

    return Handler


def main() -> int:
    import os

    if len(sys.argv) < 2:
        print("用法: python -m scripts.trace_service <port>", file=sys.stderr)
        return 2

    port = int(sys.argv[1])
    name = os.environ.get("SERVICE_NAME", f"svc{port}")
    downstream = os.environ.get("DOWNSTREAM")
    trace_log = os.environ.get("TRACE_LOG")

    tracer = Tracer(exporter=Path(trace_log) if trace_log else None)
    server = ThreadingHTTPServer(("127.0.0.1", port), build_handler(tracer, downstream, name))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"READY {name} http://127.0.0.1:{port}", flush=True)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
