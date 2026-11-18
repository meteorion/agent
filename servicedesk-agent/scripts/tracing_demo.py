# file: scripts/tracing_demo.py
"""5.5 的场景：一条 trace 串起 客户端 → 服务 A → 服务 B → MCP Server。

用法：
    uv run python -m scripts.tracing_demo
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from servicedesk.logging_setup import configure_logging
from servicedesk.mcp.http_app import create_server
from servicedesk.obs.tracing import (
    Tracer,
    build_tree,
    format_traceparent,
    inject,
    load_spans,
    new_trace,
    parse_traceparent,
    render_tree,
)


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_line(proc: subprocess.Popen[str], prefix: str, timeout_s: float = 20.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            time.sleep(0.05)
            continue
        if line.startswith(prefix):
            return line.strip()
    raise TimeoutError(f"子进程没有输出 {prefix}")


def spawn_service(*, name: str, port: int, trace_log: Path, downstream: str | None) -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "SERVICE_NAME": name,
        "TRACE_LOG": str(trace_log),
        "PYTHONIOENCODING": "utf-8",
    }
    if downstream:
        env["DOWNSTREAM"] = downstream
    return subprocess.Popen(
        [sys.executable, "-m", "scripts.trace_service", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )


# ------------------------------- 演示 -------------------------------


def demo_format() -> None:
    header("第 1 步：traceparent 就四个字段")
    ctx = new_trace()
    value = format_traceparent(ctx)
    print(f"  生成的 header: {value}")
    print()
    print("  拆开看：")
    version, trace_id, span_id, flags = value.split("-")
    print(f"    version  {version}     （固定 00）")
    print(f"    trace-id {trace_id}  （32 hex，整条链路不变）")
    print(f"    parent   {span_id}  （16 hex，本跳自己的）")
    print(f"    flags    {flags}     （01=采样）")

    parsed = parse_traceparent(value)
    print()
    print(f"  解析回来: trace_id 一致={parsed.trace_id == ctx.trace_id}  "
          f"sampled={parsed.sampled}")
    print(f"  解析结果的 span_id 就是**上游的跨度**（{parsed.span_id[:8]}…），")
    print(f"  本地要记录自己的跨度时用 `.child()`：得到 {parsed.child().span_id[:8]}…，")
    print(f"  它的 parent_id 指回上游（{parsed.child().parent_id[:8]}…）。")


def demo_child() -> None:
    header("第 2 步：派生下一跳 —— trace_id 不变，span_id 换新")
    root = new_trace()
    a = root.child()
    b = a.child()

    print(f"  root  trace={root.trace_id[:8]}… span={root.span_id[:8]}… parent=None")
    print(f"  A     trace={a.trace_id[:8]}… span={a.span_id[:8]}… parent={a.parent_id[:8]}…")
    print(f"  B     trace={b.trace_id[:8]}… span={b.span_id[:8]}… parent={b.parent_id[:8]}…")
    print()
    print(f"  三者 trace_id 相同: {root.trace_id == a.trace_id == b.trace_id}")
    print(f"  A 的 parent 是 root: {a.parent_id == root.span_id}")
    print(f"  B 的 parent 是 A   : {b.parent_id == a.span_id}")
    print()
    print("  >>> 常见误解：以为换了 span_id 就是新 trace。不是——**trace_id 一路不变**。")


def demo_malformed() -> None:
    header("第 3 步：畸形 header 要宽容处理，不能 500")
    samples = [
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",   # 合法
        "00-" + "0" * 32 + "-00f067aa0ba902b7-01",                    # 全零 trace（禁止）
        "00-xyz-00f067aa0ba902b7-01",                                 # 非法字符
        "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",   # 未知版本
        "garbage",                                                    # 完全不是格式
    ]
    for value in samples:
        parsed = parse_traceparent(value)
        status = "✅ 接受" if parsed else "⛔ 丢弃（自己起新链路）"
        print(f"  {status}  {value[:52]}")
    print()
    print("  >>> 规范要求：解析失败就当没收到。**一个畸形 header 不该让整个请求挂掉。**")


def demo_sampling() -> None:
    header("第 4 步：采样 —— 透传但不算开销")
    tracer = Tracer()
    sampled = new_trace(sampled=True)
    unsampled = new_trace(sampled=False)

    for ctx in (sampled, unsampled):
        with tracer.span("work", ctx) as span:
            span.attributes["note"] = "demo"

    print(f"  采样=true  记录 span 数: {sum(1 for s in tracer.spans if s.ctx.sampled)}")
    print(f"  采样=false 记录 span 数: {sum(1 for s in tracer.spans if not s.ctx.sampled)}")
    print(f"  tracer 里总共: {len(tracer.spans)} 个")
    print()
    print("  >>> 采样=false 的请求**仍然透传 traceparent**，只是我们不记录 span。")
    print("      这样上游仍然能拿到完整链路，只是这一跳没花钱。")


def demo_cross_service(tmp: Path) -> dict:
    header("第 5 步：跨进程 —— 客户端 → 服务 A → 服务 B")
    trace_log = tmp / "spans.jsonl"

    port_b = free_port()
    port_a = free_port()
    url_b = f"http://127.0.0.1:{port_b}"
    url_a = f"http://127.0.0.1:{port_a}"

    svc_b = spawn_service(name="svc-b", port=port_b, trace_log=trace_log, downstream=None)
    wait_line(svc_b, "READY")
    svc_a = spawn_service(name="svc-a", port=port_a, trace_log=trace_log, downstream=url_b)
    wait_line(svc_a, "READY")

    try:
        tracer = Tracer(exporter=trace_log)
        root = new_trace()

        with tracer.span("client.request", root) as span:
            span.attributes["target"] = url_a
            headers = {"Content-Type": "application/json"}
            inject(headers, root)
            with httpx.Client(timeout=10) as client:
                response = client.post(f"{url_a}/call", json={"from": "client"}, headers=headers)
            span.attributes["status"] = response.status_code
            body = response.json()

        print(f"  客户端 trace_id: {root.trace_id}")
        print(f"  A 返回   trace_id: {body['trace_id']}   一致={body['trace_id'] == root.trace_id}")
        print(f"  A 的 parent: {body['parent_id'][:8]}…  是客户端 span: {body['parent_id'] == root.span_id}")
        downstream = body.get("downstream") or {}
        print(f"  B 返回   trace_id: {downstream.get('trace_id', '')[:32]}")
        print(f"  B 的 parent: {(downstream.get('parent_id') or '')[:8]}…")
    finally:
        for proc in (svc_a, svc_b):
            proc.terminate()

    return {"trace_log": trace_log, "root": root, "port_a": port_a}


def demo_mcp(tmp: Path) -> dict:
    header("第 6 步：跨进程且含 MCP 调用")
    trace_log = tmp / "mcp-spans.jsonl"

    server, url = create_server(trace_log=str(trace_log))
    tracer = Tracer(exporter=trace_log)
    root = new_trace()

    try:
        with tracer.span("client.tool_call", root) as span:
            headers = {
                "Content-Type": "application/json",
                "Mcp-Method": "tools/call",
                "MCP-Protocol-Version": "2026-07-28",
            }
            inject(headers, root)
            payload = {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "query_asset", "arguments": {"hostname": "NB-1024"}},
            }
            with httpx.Client(timeout=10) as client:
                response = client.post(url, json=payload, headers=headers)
            span.attributes["mcp_status"] = response.status_code
            result = response.json()["result"]
    finally:
        server.shutdown()

    spans = load_spans(trace_log)
    print(f"  MCP 调用返回: isError={result['isError']}")
    print(f"  记录到的 span: {[s['name'] for s in spans]}")
    if spans:
        print(f"  span 的 parent 是客户端 span: {spans[0]['parent_id'] == root.span_id}")
    print()
    print("  >>> 这说明 2.5 的 MCP Server 也接进了同一条链路——**工具调用不再是黑盒**。")

    return {"trace_log": trace_log}


def demo_tree(spans_path: Path, label: str) -> None:
    header(f"第 7 步：把 span 还原成调用树（{label}）")
    spans = load_spans(spans_path)
    print(f"  共 {len(spans)} 个 span，{len({s['trace_id'] for s in spans})} 条 trace")
    print()
    for line in render_tree(build_tree(spans)):
        print(f"  {line}")
    print()
    print("  >>> 一眼能看出：谁调了谁、每一跳多久。")
    print("      这就是 3.6 的 trace_id（单进程）升级到全链路后的样子。")


def main() -> int:
    configure_logging(level="ERROR")
    tmp = Path(tempfile.mkdtemp(prefix="tracing-"))

    demo_format()
    demo_child()
    demo_malformed()
    demo_sampling()

    cross = demo_cross_service(tmp)
    demo_tree(cross["trace_log"], "跨服务")

    mcp = demo_mcp(tmp)
    demo_tree(mcp["trace_log"], "含 MCP 调用")

    header("小结")
    print("  1. traceparent 四字段：version / trace-id / parent-id / flags；**trace_id 一路不变**。")
    print("  2. 每跳派生子上下文：trace_id 不变、span_id 换新、parent 指向上一跳。")
    print("  3. 畸形 header 要宽容丢弃；采样=false 时**透传但不记录**。")
    print("  4. 实测：客户端 → A → B 三个进程、MCP Server 全部落在同一条 trace 上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
