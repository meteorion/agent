# file: tests/test_graph_approval.py
"""3.5 的断言：挂起、三种决定、超时、进程退出后恢复、节点重跑。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from servicedesk.graph.approval import (
    build_approval_graph,
    decide,
    open_approval_checkpointer,
    pending,
    start,
)


@pytest.fixture()
def graph_memory():
    return build_approval_graph(InMemorySaver())


# ------------------------------ 挂起 ------------------------------


async def test_start_suspends_at_gate(graph_memory) -> None:
    out = await start(graph_memory, "t-1", ticket_id="T-1001", draft="答复草稿", risk="high")
    info = out["pending"]

    assert info is not None
    assert info["next"] == ("human_gate",)
    assert info["trace"] == ["prepare"]
    assert info["payload"]["ticket_id"] == "T-1001"
    assert info["payload"]["risk"] == "high"
    assert "approve" in info["payload"]["ask"]


async def test_approval_payload_contains_deadline(graph_memory) -> None:
    out = await start(graph_memory, "t-2", ticket_id="T-2", timeout_s=600)
    expires_at = out["pending"]["payload"]["expires_at"]
    assert isinstance(expires_at, float)
    assert expires_at > 0


# ------------------------------ 三种决定 ------------------------------


async def test_approve_delivers(graph_memory) -> None:
    await start(graph_memory, "t-a", ticket_id="T-A", draft="答复 A")
    result = await decide(graph_memory, "t-a", "approve")

    assert result["decision"] == "approve"
    assert result["final"] == "已交付：答复 A"
    assert result["trace"] == ["prepare", "gate:approve", "deliver"]


async def test_reject_cancels(graph_memory) -> None:
    await start(graph_memory, "t-r", ticket_id="T-R", draft="答复 R")
    result = await decide(graph_memory, "t-r", "reject")

    assert result["decision"] == "reject"
    assert "作废" in result["final"]
    assert "cancelled" in result["trace"]


async def test_edit_replaces_draft(graph_memory) -> None:
    await start(graph_memory, "t-e", ticket_id="T-E", draft="原始答复")
    result = await decide(
        graph_memory, "t-e", {"action": "edit", "text": "人工改写后的答复"}
    )

    assert result["decision"] == "edit"
    assert "人工改写后的答复" in result["final"]
    assert "deliver" in result["trace"]


async def test_unknown_decision_is_treated_as_reject(graph_memory) -> None:
    await start(graph_memory, "t-x", ticket_id="T-X", draft="答复 X")
    result = await decide(graph_memory, "t-x", "随便说了点什么")
    assert "cancelled" in result["trace"]


# ------------------------------ 超时 ------------------------------


async def test_expired_beats_approval(graph_memory) -> None:
    """即使人点了批准，过期也算作废。"""
    await start(graph_memory, "t-exp", ticket_id="T-EXP", timeout_s=-1)
    result = await decide(graph_memory, "t-exp", "approve")

    assert result["decision"] == "approve"
    assert result["trace"][-1] == "expired"
    assert "超时" in result["final"]


# ------------------------------ 状态查询 ------------------------------


async def test_pending_is_none_after_decision(graph_memory) -> None:
    await start(graph_memory, "t-p", ticket_id="T-P")
    assert await pending(graph_memory, "t-p") is not None

    await decide(graph_memory, "t-p", "approve")
    assert await pending(graph_memory, "t-p") is None


async def test_pending_is_none_for_unknown_thread(graph_memory) -> None:
    assert await pending(graph_memory, "never-started") is None


# -------------------------- 进程退出后恢复 --------------------------


def test_suspended_state_survives_process_exit(tmp_path: Path) -> None:
    """子进程发起审批后退出；新进程从 SQLite 读到审批单并完成审批。"""
    db = tmp_path / "approval.sqlite"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    child = subprocess.run(
        [sys.executable, "-m", "scripts.approval_worker", str(db), "t-proc", "T-9001"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert child.returncode == 0, child.stderr
    assert "human_gate" in child.stdout
    assert db.exists()

    resume_code = (
        "import asyncio, json, sys\n"
        "from servicedesk.graph.approval import "
        "build_approval_graph, decide, open_approval_checkpointer, pending\n"
        "async def main():\n"
        "    async with open_approval_checkpointer(sys.argv[1]) as saver:\n"
        "        g = build_approval_graph(saver)\n"
        "        info = await pending(g, 't-proc')\n"
        "        result = await decide(g, 't-proc', 'approve')\n"
        "        print(json.dumps({'ticket': info['payload']['ticket_id'],\n"
        "                          'next': list(info['next']),\n"
        "                          'final': result['final'],\n"
        "                          'trace': result['trace']}, ensure_ascii=False))\n"
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
    assert payload["ticket"] == "T-9001"
    assert payload["next"] == ["human_gate"]
    assert "已交付" in payload["final"]
    assert payload["trace"] == ["prepare", "gate:approve", "deliver"]


# ------------------------ 节点重跑（关键坑） ------------------------


async def test_gate_node_runs_twice_on_resume() -> None:
    """被 resume 的节点会从头重跑——所以 interrupt 之前不能放副作用。"""
    calls = {"enter": 0, "side_effect": 0}

    from typing import TypedDict

    class MiniState(TypedDict, total=False):
        decision: str

    async def gate(state):  # type: ignore[no-untyped-def]
        calls["enter"] += 1
        calls["side_effect"] += 1
        answer = interrupt({"ask": "批准？"})
        return {"decision": str(answer)}

    builder = StateGraph(MiniState)
    builder.add_node("gate", gate)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t-rerun"}}

    await graph.ainvoke({}, config=config)
    await graph.ainvoke(Command(resume="approve"), config=config)

    assert calls["enter"] == 2, "节点被进入两次"
    assert calls["side_effect"] == 2, "副作用因此也发生了两次"
