# file: tests/test_graph_streaming.py
"""3.6 的断言：流式事件顺序、首条延迟、trace_id 贯穿、前端渲染。"""

from __future__ import annotations

import time

import pytest

from servicedesk.graph.streaming import (
    NODE_LABELS,
    PIPELINE_ORDER,
    ProgressEvent,
    build_pipeline_graph,
    new_trace_id,
    render_progress,
    stream_node_updates,
    stream_progress,
)

STEP = 0.01


def test_trace_id_is_unique_and_prefixed() -> None:
    first, second = new_trace_id(), new_trace_id()
    assert first.startswith("tr_")
    assert first != second


def test_progress_event_serializes_flat() -> None:
    event = ProgressEvent(at=1.234, kind="node_end", node="retrieve", detail="检索知识库 ✓", trace_id="tr_x")
    payload = event.as_dict()

    assert payload == {
        "at": 1.23,
        "kind": "node_end",
        "node": "retrieve",
        "detail": "检索知识库 ✓",
        "trace_id": "tr_x",
    }
    assert "LangGraph" not in str(payload), "不该把内部结构漏给前端"


def test_for_user_renders_step_counter() -> None:
    event = ProgressEvent(3.0, "node_end", "retrieve", "检索知识库 ✓", "tr_x")
    assert event.for_user(2, 5) == "[  3.0s] 第 2/5 步：检索知识库 ✓"
    assert event.for_user() == "[  3.0s] 检索知识库 ✓"


# ------------------------------ 流式行为 ------------------------------


async def test_stream_progress_order_and_trace_id() -> None:
    graph = build_pipeline_graph(step_seconds=STEP)
    trace_id = new_trace_id()
    events = [event async for event in stream_progress(graph, {"trace": []}, trace_id=trace_id)]

    assert events[0].kind == "graph_start"
    assert events[-1].kind == "graph_end"
    assert all(event.trace_id == trace_id for event in events)

    starts = [e.node for e in events if e.kind == "node_start"]
    ends = [e.node for e in events if e.kind == "node_end"]
    assert starts == PIPELINE_ORDER
    assert ends == PIPELINE_ORDER


async def test_first_event_arrives_before_work_finishes() -> None:
    """核心验收：首条事件远早于整体完成。"""
    graph = build_pipeline_graph(step_seconds=0.2)
    started = time.perf_counter()

    first_start: float | None = None
    async for event in stream_progress(graph, {"trace": []}):
        if event.kind == "node_start" and first_start is None:
            first_start = time.perf_counter() - started

    total = time.perf_counter() - started
    assert first_start is not None
    assert first_start < 0.15, f"首条进度 {first_start:.2f}s，应该接近 0"
    assert total >= len(PIPELINE_ORDER) * 0.2 * 0.9, f"总耗时 {total:.2f}s 应该接近 1s"


async def test_events_are_monotonic_in_time() -> None:
    graph = build_pipeline_graph(step_seconds=STEP)
    events = [event async for event in stream_progress(graph, {"trace": []})]
    times = [event.at for event in events]
    assert times == sorted(times)


async def test_stream_does_not_leak_internal_names() -> None:
    graph = build_pipeline_graph(step_seconds=STEP)
    events = [event async for event in stream_progress(graph, {"trace": []})]

    nodes = {event.node for event in events}
    assert "LangGraph" not in nodes
    assert "__start__" not in nodes
    assert "__end__" not in nodes


# ---------------------------- 简单流与渲染 ----------------------------


async def test_stream_node_updates_yields_one_chunk_per_node() -> None:
    graph = build_pipeline_graph(step_seconds=STEP)
    seen = [node async for node, _ in stream_node_updates(graph, {"trace": []})]
    assert seen == PIPELINE_ORDER


async def test_render_progress_produces_readable_lines() -> None:
    graph = build_pipeline_graph(step_seconds=STEP)
    lines = await render_progress(graph, {"trace": []})

    assert lines[0].endswith("开始处理")
    assert lines[-1].startswith("[") and "处理完成" in lines[-1]
    step_lines = [line for line in lines if "步：" in line]
    assert len(step_lines) == len(PIPELINE_ORDER)
    assert "第 1/5 步：识别问题类型 ✓" in step_lines[0]
    assert "第 5/5 步：校对并定稿 ✓" in step_lines[-1]


def test_every_pipeline_node_has_a_label() -> None:
    for node in PIPELINE_ORDER:
        assert node in NODE_LABELS, f"{node} 缺少人话标签"
