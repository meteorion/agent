# file: tests/test_tracing.py
"""5.5 的断言：traceparent 解析/派生/透传、span 树还原、采样与 MCP 集成。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from servicedesk.mcp.http_app import create_server
from servicedesk.obs.tracing import (
    TraceContext,
    Tracer,
    build_tree,
    extract,
    extract_or_new,
    format_baggage,
    format_traceparent,
    inject,
    load_spans,
    new_trace,
    parse_baggage,
    parse_traceparent,
    render_tree,
)

VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


# ------------------------------ 解析 ------------------------------


def test_parse_valid_header() -> None:
    ctx = parse_traceparent(VALID)
    assert ctx is not None
    assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ctx.span_id == "00f067aa0ba902b7", "解析结果的 span_id 是上游跨度"
    assert ctx.sampled is True


def test_parse_marks_unsampled() -> None:
    ctx = parse_traceparent(VALID[:-2] + "00")
    assert ctx is not None and ctx.sampled is False


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "garbage",
        "00-xyz-00f067aa0ba902b7-01",
        "01-" + "4bf92f3577b34da6a3ce929d0e0e4736" + "-00f067aa0ba902b7-01",
        "00-" + "0" * 32 + "-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",
    ],
)
def test_parse_rejects_malformed(value: str | None) -> None:
    """畸形 header 一律丢弃——规范要求"当没收到"，不能让请求 500。"""
    assert parse_traceparent(value) is None


def test_format_roundtrip() -> None:
    ctx = new_trace()
    parsed = parse_traceparent(format_traceparent(ctx))
    assert parsed is not None
    assert parsed.trace_id == ctx.trace_id
    assert parsed.span_id == ctx.span_id
    assert parsed.sampled == ctx.sampled


# ------------------------------ 派生 ------------------------------


def test_child_keeps_trace_and_moves_parent() -> None:
    root = new_trace()
    child = root.child()

    assert child.trace_id == root.trace_id, "trace_id 必须一路不变"
    assert child.span_id != root.span_id, "span_id 每跳换新"
    assert child.parent_id == root.span_id


def test_child_chain_forms_lineage() -> None:
    root = new_trace()
    a = root.child()
    b = a.child()

    assert root.trace_id == a.trace_id == b.trace_id
    assert a.parent_id == root.span_id
    assert b.parent_id == a.span_id


def test_unsampled_flag_propagates() -> None:
    root = new_trace(sampled=False)
    assert root.child().sampled is False


# ------------------------------ 透传 ------------------------------


def test_extract_is_case_insensitive() -> None:
    ctx = extract({"TraceParent": VALID})
    assert ctx is not None and ctx.trace_id.startswith("4bf9")


def test_extract_or_new_generates_when_missing() -> None:
    ctx = extract_or_new({})
    assert len(ctx.trace_id) == 32


def test_inject_writes_header() -> None:
    headers: dict[str, str] = {}
    ctx = new_trace()
    inject(headers, ctx)
    assert headers["traceparent"] == format_traceparent(ctx)


# ------------------------------ baggage ------------------------------


def test_baggage_roundtrip() -> None:
    values = {"user_id": "u-1", "tenant": "t-9"}
    assert parse_baggage(format_baggage(values)) == values


def test_baggage_ignores_malformed_items() -> None:
    assert parse_baggage("a=1,broken,b=2") == {"a": "1", "b": "2"}
    assert parse_baggage(None) == {}


# ------------------------------ tracer ------------------------------


def test_tracer_records_sampled_span() -> None:
    tracer = Tracer()
    ctx = new_trace()
    with tracer.span("work", ctx) as span:
        span.attributes["k"] = "v"
    assert len(tracer.spans) == 1
    assert tracer.spans[0].attributes["k"] == "v"
    assert tracer.spans[0].duration_ms >= 0


def test_tracer_skips_unsampled_span() -> None:
    tracer = Tracer()
    with tracer.span("work", new_trace(sampled=False)):
        pass
    assert tracer.spans == []


def test_tracer_records_error() -> None:
    tracer = Tracer()
    with pytest.raises(RuntimeError):
        with tracer.span("boom", new_trace()):
            raise RuntimeError("炸了")
    assert tracer.spans[0].error is not None
    assert "炸了" in (tracer.spans[0].error or "")


def test_tracer_exports_jsonl(tmp_path: Path) -> None:
    log = tmp_path / "spans.jsonl"
    tracer = Tracer(exporter=log)
    with tracer.span("a", new_trace()):
        pass
    with tracer.span("b", new_trace()):
        pass

    rows = load_spans(log)
    assert [row["name"] for row in rows] == ["a", "b"]


# ------------------------------ 树还原 ------------------------------


def _span(name: str, span_id: str, parent: str | None, started: float) -> dict:
    return {
        "name": name, "span_id": span_id, "parent_id": parent,
        "trace_id": "t" * 32, "started_at": started, "duration_ms": 1.0,
        "attributes": {}, "events": [], "error": None,
    }


def test_build_tree_links_children() -> None:
    roots = build_tree([
        _span("root", "s1", None, 1.0),
        _span("child", "s2", "s1", 2.0),
        _span("grand", "s3", "s2", 3.0),
    ])
    assert len(roots) == 1
    assert roots[0]["name"] == "root"
    assert roots[0]["children"][0]["name"] == "child"
    assert roots[0]["children"][0]["children"][0]["name"] == "grand"


def test_orphan_becomes_root() -> None:
    """跨进程时某一跳没落盘是常态——不能因此丢数据。"""
    roots = build_tree([_span("orphan", "s9", "missing", 1.0)])
    assert len(roots) == 1 and roots[0]["name"] == "orphan"


def test_render_tree_indents() -> None:
    roots = build_tree([
        _span("root", "s1", None, 1.0),
        _span("child", "s2", "s1", 2.0),
    ])
    lines = render_tree(roots)
    assert lines[0].startswith("root")
    assert lines[1].startswith("  └─ child")


# ------------------------------ MCP 集成 ------------------------------


async def test_mcp_server_records_span_with_client_parent(tmp_path: Path) -> None:
    """验收点：MCP 调用落在同一条 trace 上，且 parent 指向客户端 span。"""
    log = tmp_path / "mcp.jsonl"
    server, url = create_server(trace_log=str(log))
    try:
        tracer = Tracer()
        root = new_trace()
        with tracer.span("client.tool_call", root):
            headers = {
                "Content-Type": "application/json",
                "Mcp-Method": "tools/list",
                "MCP-Protocol-Version": "2026-07-28",
            }
            inject(headers, root)
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers=headers,
                )
        assert response.status_code == 200
    finally:
        server.shutdown()

    spans = load_spans(log)
    assert len(spans) == 1
    assert spans[0]["trace_id"] == root.trace_id
    assert spans[0]["parent_id"] == root.span_id
