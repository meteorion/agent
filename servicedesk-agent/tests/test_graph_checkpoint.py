# file: tests/test_graph_checkpoint.py
"""3.4 的断言：内存恢复、真实杀进程恢复、时间旅行、schema 兼容。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from langgraph.graph import END, START, StateGraph

from servicedesk.graph.checkpoint import (
    TOTAL_STEPS,
    WorkState,
    build_workflow_graph,
    ensure_defaults,
    finish,
    history,
    make_memory_checkpointer,
    make_step,
    open_sqlite_checkpointer,
    replay,
    run,
    snapshot,
    thread_config,
)


def test_thread_config_shape() -> None:
    assert thread_config("abc") == {"configurable": {"thread_id": "abc"}}


# ------------------------------ 内存恢复 ------------------------------


def build_fragile_graph(saver):
    """第 3 步一定抛异常，用来验证"前两步的成果被保住"。"""

    async def failing_step3(state):  # type: ignore[no-untyped-def]
        raise RuntimeError("下游 500")

    builder = StateGraph(WorkState)
    builder.add_node("step1", make_step(1))
    builder.add_node("step2", make_step(2))
    builder.add_node("step3", failing_step3)
    builder.add_node("step4", make_step(4))
    builder.add_node("finish", finish)
    builder.add_edge(START, "step1")
    builder.add_edge("step1", "step2")
    builder.add_edge("step2", "step3")
    builder.add_edge("step3", "step4")
    builder.add_edge("step4", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=saver)


async def test_failure_preserves_completed_steps() -> None:
    saver = make_memory_checkpointer()
    graph = build_fragile_graph(saver)

    with pytest.raises(RuntimeError):
        await run(graph, "t-fail", {"ticket_id": "T-1", "trace": []})

    snap = await snapshot(graph, "t-fail")
    assert snap["step"] == 2
    assert snap["trace"] == ["step1", "step2"]
    assert snap["next"] == ("step3",)


async def test_resume_continues_without_restarting() -> None:
    saver = make_memory_checkpointer()
    fragile = build_fragile_graph(saver)
    with pytest.raises(RuntimeError):
        await run(fragile, "t-resume", {"ticket_id": "T-1", "trace": []})

    healed = build_workflow_graph(saver)
    out = await run(healed, "t-resume")

    assert out["step"] == TOTAL_STEPS
    assert out["trace"] == ["step1", "step2", "step3", "step4"]
    assert "处理完成" in out["answer"]


async def test_different_threads_are_isolated() -> None:
    saver = make_memory_checkpointer()
    graph = build_workflow_graph(saver)

    await run(graph, "thread-a", {"ticket_id": "A", "trace": []})
    await run(graph, "thread-b", {"ticket_id": "B", "trace": []})

    snap_a = await snapshot(graph, "thread-a")
    snap_b = await snapshot(graph, "thread-b")
    assert snap_a["step"] == snap_b["step"] == TOTAL_STEPS
    assert snap_a["checkpoint_id"] != snap_b["checkpoint_id"]


# ------------------------------ 磁盘持久化 ------------------------------


async def test_sqlite_roundtrip_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "ckpt.sqlite"

    async with open_sqlite_checkpointer(db) as saver:
        graph = build_workflow_graph(saver)
        await run(graph, "t-sqlite", {"ticket_id": "T-9", "trace": []})

    # 关闭连接、重新打开 —— 模拟服务重启
    async with open_sqlite_checkpointer(db) as saver:
        graph = build_workflow_graph(saver)
        snap = await snapshot(graph, "t-sqlite")

    assert snap["step"] == TOTAL_STEPS
    assert snap["trace"] == ["step1", "step2", "step3", "step4"]


async def test_history_lists_snapshots_newest_first(tmp_path: Path) -> None:
    async with open_sqlite_checkpointer(tmp_path / "h.sqlite") as saver:
        graph = build_workflow_graph(saver)
        await run(graph, "t-history", {"ticket_id": "T-2", "trace": []})
        rows = await history(graph, "t-history")

    assert len(rows) >= TOTAL_STEPS + 1
    steps = [row["step"] for row in rows]
    assert steps == sorted(steps, reverse=True), "新的快照应该在前"


async def test_replay_from_earlier_checkpoint(tmp_path: Path) -> None:
    async with open_sqlite_checkpointer(tmp_path / "r.sqlite") as saver:
        graph = build_workflow_graph(saver)
        await run(graph, "t-replay", {"ticket_id": "T-3", "trace": []})
        rows = await history(graph, "t-replay")
        target = next(row for row in rows if row["step"] == 2)

        replayed = await replay(graph, "t-replay", target["checkpoint_id"])

    assert replayed["step"] == TOTAL_STEPS
    assert replayed["trace"] == ["step1", "step2", "step3", "step4"]


# ---------------------- 真实杀进程（本节验收点） ----------------------


def test_killed_process_resumes_from_last_completed_node(tmp_path: Path) -> None:
    """子进程在第 3 步 os._exit(1)，重启后从 step2 之后继续。"""
    db = tmp_path / "killed.sqlite"
    env = {**os.environ, "CKPT_CRASH_AT": "3", "PYTHONIOENCODING": "utf-8"}

    child = subprocess.run(
        [sys.executable, "-m", "scripts.checkpoint_worker", str(db), "t-killed"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert child.returncode != 0, "子进程应该被中断"
    assert db.exists(), "中断前应该已经落盘"

    resume_code = (
        "import asyncio, json, sys\n"
        "from servicedesk.graph.checkpoint import "
        "build_workflow_graph, open_sqlite_checkpointer, run, snapshot\n"
        "async def main():\n"
        "    async with open_sqlite_checkpointer(sys.argv[1]) as saver:\n"
        "        g = build_workflow_graph(saver)\n"
        "        before = await snapshot(g, 't-killed')\n"
        "        out = await run(g, 't-killed')\n"
        "        print(json.dumps({'before': before['step'], 'next': list(before['next']),\n"
        "                          'after': out['step'], 'trace': out['trace']}, ensure_ascii=False))\n"
        "asyncio.run(main())\n"
    )
    resumed = subprocess.run(
        [sys.executable, "-c", resume_code, str(db)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert resumed.returncode == 0, resumed.stderr

    import json

    payload = json.loads(resumed.stdout.strip().splitlines()[-1])
    assert payload["before"] == 2, "断点应停在第 2 步之后"
    assert payload["next"] == ["step3"], "下一步应该是被中断的 step3"
    assert payload["after"] == TOTAL_STEPS
    assert payload["trace"] == ["step1", "step2", "step3", "step4"]


# ------------------------------ schema 兼容 ------------------------------


def test_ensure_defaults_fills_missing_fields() -> None:
    old = {"ticket_id": "T-3001", "trace": ["step1"]}
    filled = ensure_defaults(old)

    assert filled["step"] == 0
    assert filled["answer"] == ""
    assert filled["error"] is None
    assert filled["trace"] == ["step1"]


def test_ensure_defaults_keeps_existing_values() -> None:
    filled = ensure_defaults({"step": 3, "answer": "完成", "trace": ["a"]})
    assert filled["step"] == 3
    assert filled["answer"] == "完成"
    assert filled["trace"] == ["a"]
