# file: scripts/streaming_demo.py
"""3.6 的场景：阻塞 vs 流式，以及 trace_id 贯穿。

用法：
    uv run python -m scripts.streaming_demo          # 默认每步 3 秒，全程 15 秒
    STEP_SECONDS=0.2 uv run python -m scripts.streaming_demo   # 快速跑一遍
"""

from __future__ import annotations

import asyncio
import os
import time

from servicedesk.graph.streaming import (
    NODE_LABELS,
    PIPELINE_ORDER,
    build_pipeline_graph,
    new_trace_id,
    render_progress,
    stream_node_updates,
    stream_progress,
)
from servicedesk.logging_setup import configure_logging

STEP_SECONDS = float(os.environ.get("STEP_SECONDS", "3.0"))


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


async def demo_blocking() -> dict:
    header("第 1 步：阻塞调用 —— 15 秒里用户什么都看不到")
    graph = build_pipeline_graph(step_seconds=STEP_SECONDS)
    started = time.perf_counter()

    out = await graph.ainvoke({"ticket_id": "T-1001", "trace": []})

    elapsed = time.perf_counter() - started
    print(f"  第一次看到输出: {elapsed:.1f}s（就在全部跑完之后）")
    print(f"  结果: stage={out['stage']} answer={out['answer']}")
    print(f"  >>> 用户盯着转圈 {elapsed:.0f} 秒。他甚至不知道卡在哪一步。")
    return {"elapsed": elapsed, "out": out}


async def demo_streaming() -> list:
    header("第 2 步：流式调用 —— 第一条事件在毫秒级到达")
    graph = build_pipeline_graph(step_seconds=STEP_SECONDS)
    trace_id = new_trace_id()

    print(f"  trace_id: {trace_id}")
    print()
    first_at: float | None = None
    started = time.perf_counter()
    events = []

    async for event in stream_progress(graph, {"ticket_id": "T-1001", "trace": []}, trace_id=trace_id):
        events.append(event)
        if first_at is None and event.kind == "node_start":
            first_at = time.perf_counter() - started
        marker = "  ◄ 用户第一次看到进度" if event.kind == "node_start" and first_at is not None and len(events) <= 3 else ""
        print(f"  {event.at:5.1f}s  {event.kind:<11} {NODE_LABELS.get(event.node, event.node):<12} {event.detail}{marker}")

    total = time.perf_counter() - started
    print()
    print(f"  首条进度: {first_at:.2f}s    全部完成: {total:.1f}s（与阻塞版相同）")
    print(f"  >>> 总耗时没变，但**用户从第 {first_at:.1f} 秒起就知道在干什么了**。")
    return events


async def demo_node_updates() -> None:
    header("第 3 步：更简单的流 —— astream(stream_mode=updates)")
    graph = build_pipeline_graph(step_seconds=STEP_SECONDS / 5)
    print("  每跑完一个节点产出一条增量：")
    async for node, update in stream_node_updates(graph, {"trace": []}):
        keys = ", ".join(k for k in update if k != "trace")
        print(f"    {node:<10} → 更新字段: {keys}")
    print()
    print("  >>> 这种流最省事：不关心开始/结束，只要「谁跑完了」。")
    print("      想画进度条、想给工具调用单独计时，才需要 astream_events。")


async def demo_render() -> None:
    header("第 4 步：渲染成给用户看的进度条文字")
    graph = build_pipeline_graph(step_seconds=STEP_SECONDS / 5)
    lines = await render_progress(graph, {"trace": []})
    for line in lines:
        print(f"  {line}")
    print()
    print("  >>> 前端拿到的是纯文本行，不用理解 LangGraph 的任何内部结构。")


async def demo_compare(blocking: dict) -> None:
    header("第 5 步：两种调用的体验对比")
    total = blocking["elapsed"]
    print(f"  阻塞版：0.0s 开始 → {total:.1f}s 一次性出结果")
    print(f"          用户可见进度点：1 个（在最后）")
    print()
    print(f"  流式版：0.0s 开始 → 0.0s 第一条进度 → 每 {STEP_SECONDS:.0f}s 一条 → {total:.1f}s 完成")
    print(f"          用户可见进度点：{len(PIPELINE_ORDER) + 2} 个")
    print()
    print("  >>> 做过客服系统的人都知道：用户抱怨的往往不是「慢」，")
    print("      而是「不知道还要等多久」。流式解决的正是后半句。")


async def main() -> int:
    configure_logging(level="WARNING")     # demo 里关掉逐条日志，只看界面输出
    print(f"每步耗时: {STEP_SECONDS}s，共 {len(PIPELINE_ORDER)} 步")

    blocking = await demo_blocking()
    await demo_streaming()
    await demo_node_updates()
    await demo_render()
    await demo_compare(blocking)

    header("小结")
    print("  1. astream(updates)：节点级增量，画进度条够用。")
    print("  2. astream_events(v2)：细粒度（开始/结束/工具），要单独计时或展示工具调用时才用。")
    print("  3. 包装成扁平事件，前端不必理解 LangGraph 的内部结构。")
    print("  4. trace_id 通过 contextvars 挂到每条日志上，事件里也带着——一次请求一条线。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
