# file: tests/test_single_vs_multi.py
"""4.1 的断言：两套实现都可跑、指标可比较、判分器能识别失败。"""

from __future__ import annotations

from servicedesk.multiagent.single_vs_multi import (
    TASKS,
    Task,
    build_multi_agent_graph,
    build_single_agent_graph,
    compare,
    compare_tool_context,
    run_multi,
    run_single,
)


async def test_single_agent_handles_all_tasks() -> None:
    result = await run_single(TASKS)
    assert result.success == result.total == len(TASKS)
    assert result.failures == []
    assert result.handoffs == 0
    assert result.context_bytes == 0


async def test_multi_agent_handles_all_tasks() -> None:
    result = await run_multi(TASKS)
    assert result.success == result.total
    assert result.handoffs > 0
    assert result.context_bytes > 0


async def test_multi_agent_costs_more_handoffs() -> None:
    single = await run_single(TASKS)
    multi = await run_multi(TASKS)

    assert single.handoffs == 0
    assert multi.handoffs >= len(TASKS) * 2, "每个任务至少 researcher→supervisor→writer→…"


async def test_multi_agent_passes_more_context() -> None:
    single = await run_single(TASKS)
    multi = await run_multi(TASKS)

    assert single.context_bytes == 0
    assert multi.context_bytes > 1000, "交接上下文应该是个可观的数字"


async def test_multi_agent_needs_recursion_limit_for_loops() -> None:
    """supervisor↔worker 来回跳是多跳循环，默认上限远大于所需，但必须能跑完。"""
    graph = build_multi_agent_graph()
    out = await graph.ainvoke(
        {"ticket_id": "T-X", "title": "VPN 认证失败", "description": "密码过期",
         "trace": [], "messages": []},
        {"recursion_limit": 40},
    )
    assert out["final"]
    assert any(t.startswith("handoff:") for t in out["trace"])


async def test_compare_returns_both_engines() -> None:
    data = await compare()
    assert set(data) == {"single", "multi"}
    for engine in ("single", "multi"):
        assert data[engine]["total"] == len(TASKS)
        assert 0.0 <= data[engine]["success_rate"] <= 1.0
        assert "failures" in data[engine]


async def test_scorer_detects_wrong_answer() -> None:
    """判分器必须真的能判错，否则上面的"100% 通过"没有意义。"""
    impossible = [
        Task("T-bad", "VPN 连不上", "认证失败", "vpn", "这句话不会出现在任何答复里", True)
    ]
    single = await run_single(impossible)
    multi = await run_multi(impossible)

    assert single.success == 0 and single.failures
    assert multi.success == 0 and multi.failures


# ---------------------------- 工具上下文场景 ----------------------------


def test_multi_agent_saves_tool_context() -> None:
    data = compare_tool_context()

    assert data["工具总数"] >= 3
    assert data["单 Agent 三次调用合计"] > data["多 Agent 合计（含路由清单 x3）"]
    assert data["节省比例"] > 0.5, "本节实测约 61%"


def test_tool_context_groups_cover_all_tools() -> None:
    from servicedesk.tools.builtin import build_default_registry

    names = {item["function"]["name"] for item in build_default_registry().schemas()}
    data = compare_tool_context()

    grouped = sum(len(group) for group in [data["多 Agent 各组每次"].keys()])
    assert grouped == 3, "三个分组"
    assert data["工具总数"] == len(names)


def test_single_graph_is_linear() -> None:
    graph = build_single_agent_graph()
    nodes = [n for n in graph.get_graph().nodes if not n.startswith("__")]
    assert nodes == ["classify", "retrieve", "draft", "review"]


def test_multi_graph_has_supervisor_and_workers() -> None:
    graph = build_multi_agent_graph()
    nodes = {n for n in graph.get_graph().nodes if not n.startswith("__")}
    assert nodes == {"supervisor", "researcher", "writer", "reviewer"}
