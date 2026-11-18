# file: tests/test_topologies.py
"""4.3 的断言：两种拓扑都能完成、轮次差异可量化、循环有止损。"""

from __future__ import annotations

import pytest
from langgraph.errors import GraphRecursionError

from servicedesk.multiagent.topologies import (
    TASKS,
    _route_supervisor,
    build_handoff_graph,
    build_supervisor_graph,
    compare_topologies,
)


def payload(index: int = 0) -> dict:
    ticket_id, title, description, _ = TASKS[index]
    return {"ticket_id": ticket_id, "title": title, "description": description,
            "trace": [], "notes": []}


# ------------------------------ 结构 ------------------------------


def test_supervisor_has_back_edges_to_center() -> None:
    graph = build_supervisor_graph()
    edges = {(e.source, e.target) for e in graph.get_graph().edges}

    for worker in ("triage", "research", "write", "review"):
        assert (worker, "supervisor") in edges, f"{worker} 应该回到中心"
        assert ("supervisor", worker) in edges


def test_handoff_hides_jumps_from_edge_table() -> None:
    """handoff 的跳转写在 Command 里，边表看不到——这是它的主要缺点。"""
    graph = build_handoff_graph()
    edges = [
        (e.source, e.target)
        for e in graph.get_graph().edges
        if not e.source.startswith("__") and not e.target.startswith("__")
    ]
    assert edges == []


# ------------------------------ 路由 ------------------------------


@pytest.mark.parametrize(
    ("stage", "expected"),
    [("", "triage"), ("triaged", "research"), ("researched", "write"),
     ("written", "review"), ("reviewed", "finalize"), ("finalized", "finalize")],
)
def test_supervisor_routes_by_explicit_stage(stage: str, expected: str) -> None:
    assert _route_supervisor({"stage": stage}) == expected


def test_missing_stage_does_not_loop() -> None:
    """回归：曾用"字段是否为空"推断进度，无资料的任务会死循环。"""
    assert _route_supervisor({}) == "triage"


# ------------------------------ 端到端 ------------------------------


async def test_supervisor_handles_all_tasks() -> None:
    graph = build_supervisor_graph()
    for index in range(len(TASKS)):
        out = await graph.ainvoke(payload(index), {"recursion_limit": 40})
        assert out["final"]
        assert out["stage"] == "finalized"


async def test_handoff_handles_all_tasks() -> None:
    graph = build_handoff_graph()
    for index in range(len(TASKS)):
        out = await graph.ainvoke(payload(index), {"recursion_limit": 40})
        assert out["final"]


async def test_supervisor_costs_about_double_turns() -> None:
    data = await compare_topologies()
    sup = data["supervisor"]["turns_per_task"]
    ho = data["handoff"]["turns_per_task"]

    assert sup == pytest.approx(ho * 2, rel=0.01), (
        f"supervisor 每跳多一次回中心：sup={sup} ho={ho}"
    )


async def test_both_topologies_succeed_on_same_tasks() -> None:
    data = await compare_topologies()
    assert data["supervisor"]["success"] == data["handoff"]["success"] == len(TASKS)
    assert data["supervisor"]["failures"] == []
    assert data["handoff"]["failures"] == []


# ------------------------------ 止损 ------------------------------


async def test_handoff_pingpong_stops_itself() -> None:
    """无资料的任务会让 review 反复打回 write，必须自己止损。"""
    graph = build_handoff_graph()
    out = await graph.ainvoke(payload(2), {"recursion_limit": 40})

    assert out["visits"]["write"] == 3
    assert out["visits"]["review"] == 2
    assert "止损" in out["final"]
    assert out["trace"][-1] == "writer:abort"


async def test_handoff_without_guard_would_hit_recursion_limit() -> None:
    """反证：把止损条件放开，就会撞到递归上限（说明 guard 是必需的）。"""
    graph = build_handoff_graph()
    with pytest.raises(GraphRecursionError):
        await graph.ainvoke(payload(2), {"recursion_limit": 6})


async def test_handoff_turns_are_half_of_supervisor() -> None:
    supervisor = build_supervisor_graph()
    handoff = build_handoff_graph()

    sup_out = await supervisor.ainvoke(payload(0), {"recursion_limit": 40})
    ho_out = await handoff.ainvoke(payload(0), {"recursion_limit": 40})

    assert sup_out["turns"] == 10
    assert ho_out["turns"] == 4
    assert len(sup_out["trace"]) == 10
    assert len(ho_out["trace"]) == 4
