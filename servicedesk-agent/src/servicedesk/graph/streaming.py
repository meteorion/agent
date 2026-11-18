# file: src/servicedesk/graph/streaming.py
"""流式输出与可观测：把「图在干嘛」讲给用户。

两种流，用途不同：

    astream(stream_mode="updates")   —— 节点级：谁跑完了、产出了什么（简单、够用）
    astream_events(version="v2")     —— 细粒度：节点开始/结束、工具调用、token 流

本模块把两者包装成**前端友好的事件对象**，并让 `trace_id` 贯穿日志与事件。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, TypedDict

from langgraph.graph import END, START, StateGraph

from servicedesk.logging_setup import bind_request_context, clear_request_context, get_logger

log = get_logger("graph.stream")


# ------------------------------- 事件模型 -------------------------------


def new_trace_id() -> str:
    return f"tr_{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class ProgressEvent:
    """一条可推给前端的事件。字段刻意做得扁平——前端不该解析 LangGraph 的内部结构。"""

    at: float            # 相对开始的秒数
    kind: str            # graph_start / node_start / node_end / graph_end
    node: str
    detail: str
    trace_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": round(self.at, 2),
            "kind": self.kind,
            "node": self.node,
            "detail": self.detail,
            "trace_id": self.trace_id,
        }

    def for_user(self, index: int | None = None, total: int | None = None) -> str:
        prefix = f"第 {index}/{total} 步：" if index is not None and total is not None else ""
        return f"[{self.at:5.1f}s] {prefix}{self.detail}"


# ------------------------------- 演示用图 -------------------------------


class PipelineState(TypedDict, total=False):
    ticket_id: str
    stage: str
    answer: str
    trace: list[str]


# 节点 → 人话（前端展示、日志都用它，避免把内部节点名丢给用户）
NODE_LABELS: dict[str, str] = {
    "classify": "识别问题类型",
    "retrieve": "检索知识库",
    "rerank": "重排候选段落",
    "draft": "生成答复草稿",
    "finalize": "校对并定稿",
}

PIPELINE_ORDER = ["classify", "retrieve", "rerank", "draft", "finalize"]


def build_pipeline_graph(*, step_seconds: float = 3.0):
    """5 个串行节点，每个睡 `step_seconds`。默认 3 秒 → 全跑完 15 秒。"""

    def make_node(name: str):
        async def node(state: PipelineState) -> dict[str, Any]:
            import asyncio

            await asyncio.sleep(step_seconds)   # 模拟真实耗时（检索 / LLM 生成）
            return {
                "stage": name,
                "trace": [*(state.get("trace") or []), name],
                **({"answer": "已完成的答复"} if name == "finalize" else {}),
            }

        return node

    builder = StateGraph(PipelineState)
    for name in PIPELINE_ORDER:
        builder.add_node(name, make_node(name))

    builder.add_edge(START, PIPELINE_ORDER[0])
    for left, right in zip(PIPELINE_ORDER, PIPELINE_ORDER[1:]):
        builder.add_edge(left, right)
    builder.add_edge(PIPELINE_ORDER[-1], END)

    return builder.compile()


# ------------------------------- 两种流 -------------------------------


async def stream_node_updates(
    graph: Any, payload: dict[str, Any], *, config: dict[str, Any] | None = None
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """最简单的流：每跑完一个节点，产出 `(节点名, 该节点的增量)`。"""
    async for chunk in graph.astream(payload, config=config, stream_mode="updates"):
        for node, update in (chunk or {}).items():
            yield node, update


async def stream_progress(
    graph: Any,
    payload: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> AsyncIterator[ProgressEvent]:
    """把 `astream_events` 的原始事件翻译成 `ProgressEvent`。

    为什么要翻译：原始事件有 10+ 种 `on_*` 类型、字段形状各异，
    前端不该被迫理解 LangGraph 的内部结构。这里统一成 4 种 kind。
    """
    trace_id = trace_id or new_trace_id()
    bind_request_context(trace_id=trace_id)
    started = time.perf_counter()

    try:
        yield ProgressEvent(0.0, "graph_start", "", "开始处理", trace_id)

        async for event in graph.astream_events(payload, config=config, version="v2"):
            kind = event.get("event")
            name = str(event.get("name") or "")
            if name in ("LangGraph", "__start__", "__end__"):
                continue

            at = time.perf_counter() - started
            if kind == "on_chain_start":
                label = NODE_LABELS.get(name, name)
                log.info("node_start", node=name, at=round(at, 2))
                yield ProgressEvent(at, "node_start", name, f"{label}…", trace_id)
            elif kind == "on_chain_end":
                label = NODE_LABELS.get(name, name)
                log.info("node_end", node=name, at=round(at, 2))
                yield ProgressEvent(at, "node_end", name, f"{label} ✓", trace_id)

        total = time.perf_counter() - started
        log.info("graph_end", at=round(total, 2))
        yield ProgressEvent(total, "graph_end", "", f"处理完成，用时 {total:.1f}s", trace_id)
    finally:
        clear_request_context()


async def render_progress(
    graph: Any,
    payload: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> list[str]:
    """把流式事件渲染成「进度条文字」，前端直接贴到界面上就行。"""
    lines: list[str] = []
    finished = 0
    total = len(PIPELINE_ORDER)

    async for event in stream_progress(graph, payload, config=config, trace_id=trace_id):
        if event.kind == "node_end":
            finished += 1
            lines.append(event.for_user(finished, total))
        elif event.kind in ("graph_start", "graph_end"):
            lines.append(event.for_user())
    return lines
