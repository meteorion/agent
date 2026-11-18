# file: tests/test_graph_composite.py
"""3.7 的断言：子图可独立运行、父图能组合、interrupt 冒泡、以及重跑行为。"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from servicedesk.graph.composite import (
    build_approval_subgraph,
    build_composite_graph,
    build_composite_via_callable,
    build_retrieval_subgraph,
    describe_nested,
    pending_interrupt,
    resume_flow,
    run_flow,
    thread_config,
)


# ------------------------------ 子图可独立 ------------------------------


async def test_retrieval_subgraph_runs_alone() -> None:
    out = await build_retrieval_subgraph().ainvoke(
        {"title": "VPN 连不上", "description": "认证失败", "trace": []}
    )
    assert out["intent"] == "vpn"
    assert out["docs"]
    assert out["trace"] == ["classify", "fetch", "draft"]


async def test_retrieval_subgraph_asks_when_no_docs() -> None:
    out = await build_retrieval_subgraph().ainvoke(
        {"title": "打印机坏了", "description": "不出纸", "trace": []}
    )
    assert out["intent"] == "other"
    assert out["draft"] == ""
    assert out["error"]


async def test_approval_subgraph_short_circuits_without_draft() -> None:
    """没有草稿就不该挂起——子图的短路逻辑能单独验证。"""
    out = await build_approval_subgraph().ainvoke({"ticket_id": "T-1", "draft": "", "trace": []})
    assert out["decision"] == "skipped"
    assert "补充信息" in out["final"]


# ------------------------------ 父图结构 ------------------------------


def test_parent_has_two_nodes_and_one_edge() -> None:
    info = describe_nested(build_composite_graph())
    assert info["parent_nodes"] == ["retrieval", "approval"]
    assert info["parent_edges"] == [("retrieval", "approval")]


def test_xray_exposes_subgraph_internals() -> None:
    info = describe_nested(build_composite_graph())
    nested = info.get("nested_nodes") or []
    assert any("retrieval" in name and "classify" in name for name in nested)
    assert any("approval" in name and "gate" in name for name in nested)
    assert len(nested) > len(info["parent_nodes"])


# ---------------------------- 端到端与审批 ----------------------------


async def test_interrupt_bubbles_up_to_parent() -> None:
    graph = build_composite_graph(InMemorySaver())
    await run_flow(graph, "t-1", ticket_id="T-1", title="VPN 认证失败", description="认证失败")

    info = await pending_interrupt(graph, "t-1")
    assert info is not None
    assert info["next"] == ("approval",), "next 应指向父图的子图节点"
    assert info["payload"]["ticket_id"] == "T-1"
    assert info["payload"]["draft"]


async def test_approve_delivers() -> None:
    graph = build_composite_graph(InMemorySaver())
    await run_flow(graph, "t-a", ticket_id="T-A", title="VPN 认证失败", description="认证失败")
    out = await resume_flow(graph, "t-a", "approve")

    assert out["decision"] == "approve"
    assert out["final"].startswith("已交付：")
    assert "approval" in out["stages"]


async def test_reject_cancels() -> None:
    graph = build_composite_graph(InMemorySaver())
    await run_flow(graph, "t-r", ticket_id="T-R", title="VPN 认证失败", description="认证失败")
    out = await resume_flow(graph, "t-r", "reject")

    assert out["decision"] == "reject"
    assert "转人工" in out["final"]


async def test_no_docs_never_suspends() -> None:
    graph = build_composite_graph(InMemorySaver())
    await run_flow(graph, "t-n", ticket_id="T-N", title="打印机坏了", description="不出纸")

    assert await pending_interrupt(graph, "t-n") is None
    state = await graph.aget_state(thread_config("t-n"))
    assert "补充信息" in (state.values.get("final") or "")


# ---------------------------- 另一种接法 ----------------------------


async def test_callable_variant_also_works() -> None:
    graph = build_composite_via_callable(InMemorySaver())
    await run_flow(graph, "t-c", ticket_id="T-C", title="VPN 认证失败", description="认证失败")

    info = await pending_interrupt(graph, "t-c")
    assert info is not None

    out = await resume_flow(graph, "t-c", "approve")
    assert out["final"].startswith("已交付：")
    # 包装函数把子图内部 trace 压成了一条
    assert "retrieval(callable)" in out["trace"]


# -------------------- 已知行为：前置子图会被重跑 --------------------


async def test_prerequisite_subgraph_replays_after_resume() -> None:
    """本版本 LangGraph 的行为：子图内 interrupt 后恢复，父图会重跑前置兄弟节点。

    实测：`stages` 里 `retrieval` 出现 2 次，`trace` 里 classify/fetch/draft 各出现 2 次。
    `durability` 的 async/sync/exit 三种都试过，都重跑。

    这个断言把行为**固定下来**：
      - 如果哪天 LangGraph 修好了，这条会失败 —— 那是好事，记得回来更新 3.7 正文。
      - 在此之前，读者必须知道"前置节点要可重跑"。

    规避手段：前置节点纯读、副作用放到审批之后、或干脆不嵌子图（3.5 的平图不重跑）。
    """
    graph = build_composite_graph(InMemorySaver())
    await run_flow(graph, "t-replay", ticket_id="T-RP", title="VPN 认证失败", description="认证失败")
    out = await resume_flow(graph, "t-replay", "approve")

    assert out["stages"].count("retrieval") == 2
    assert out["trace"].count("classify") == 2
    assert out["trace"].count("deliver") == 1, "出口节点只应执行一次"


async def test_replay_does_not_duplicate_final_delivery() -> None:
    """重跑的是前置节点，不是交付节点——用户不会收到两条交付。"""
    graph = build_composite_graph(InMemorySaver())
    await run_flow(graph, "t-once", ticket_id="T-O", title="VPN 认证失败", description="认证失败")
    out = await resume_flow(graph, "t-once", "approve")

    assert out["trace"].count("deliver") == 1
    assert out["trace"].count("gate:approve") == 1
