# file: tests/test_graph_minimal.py
"""3.1 的断言：State 集中、Node 纯函数、Edge 显式、编译产物可执行。"""

from __future__ import annotations

import pytest

from servicedesk.graph.minimal import (
    TicketState,
    build_minimal_graph,
    classify,
    describe_graph,
    draft,
    retrieve,
)


def state(**kwargs) -> TicketState:
    base: TicketState = {"trace": []}
    base.update(kwargs)  # type: ignore[typeddict-item]
    return base


# -------------------------------- Node --------------------------------


@pytest.mark.parametrize(
    ("title", "description", "expected"),
    [
        ("VPN 连不上", "认证失败", "vpn"),
        ("邮件发不出", "附件太大", "email"),
        ("打印机坏了", "三楼不出纸", "other"),
    ],
)
def test_classify(title: str, description: str, expected: str) -> None:
    out = classify(state(title=title, description=description))
    assert out["intent"] == expected
    assert out["trace"] == ["classify"]


def test_classify_does_not_mutate_input() -> None:
    """Node 必须是纯函数：读 state、返回要更新的字段。"""
    original = state(title="VPN 连不上", description="认证失败")
    snapshot = dict(original)
    classify(original)
    assert original == snapshot


def test_retrieve_uses_intent() -> None:
    out = retrieve(state(intent="vpn"))
    assert out["docs"] and "重置" in " ".join(out["docs"])
    assert retrieve(state(intent="other"))["docs"] == []


def test_draft_sets_approval_for_vpn_with_reset() -> None:
    vpn = draft(state(intent="vpn", docs=["处理顺序：先在自助门户重置密码。"]))
    assert vpn["needs_approval"] is True

    email = draft(state(intent="email", docs=["附件上限 25 MB。"]))
    assert email["needs_approval"] is False


def test_draft_without_docs_says_not_found() -> None:
    out = draft(state(intent="other", docs=[]))
    assert "没有相关信息" in out["draft"]


# -------------------------------- Graph --------------------------------


async def test_graph_runs_three_nodes_in_order() -> None:
    graph = build_minimal_graph()
    out = await graph.ainvoke(
        {"title": "VPN 连不上", "description": "认证失败", "trace": []}
    )

    assert out["trace"] == ["classify", "retrieve", "draft"]
    assert out["intent"] == "vpn"
    assert out["draft"]
    assert out["needs_approval"] is True


async def test_graph_is_reusable_without_state_leak() -> None:
    """编译后的图可以反复调用，每次都是干净的状态。"""
    graph = build_minimal_graph()
    first = await graph.ainvoke({"title": "VPN 连不上", "description": "认证失败", "trace": []})
    second = await graph.ainvoke({"title": "打印机坏了", "description": "不出纸", "trace": []})

    assert first["intent"] == "vpn"
    assert second["intent"] == "other"
    assert second["trace"] == ["classify", "retrieve", "draft"]
    assert second["docs"] == []


async def test_other_intent_still_walks_all_three_nodes() -> None:
    """直线图没有分支：无论意图如何都走三个节点。分支在 3.3。"""
    graph = build_minimal_graph()
    out = await graph.ainvoke({"title": "打印机坏了", "description": "不出纸", "trace": []})
    assert out["trace"] == ["classify", "retrieve", "draft"]


def test_describe_graph_lists_nodes_edges_and_entry() -> None:
    graph = build_minimal_graph()
    info = describe_graph(graph)

    assert info["nodes"] == ["classify", "retrieve", "draft"]
    assert info["edges"] == [("classify", "retrieve"), ("retrieve", "draft")]
    assert info["entry_edges"] == [("__start__", "classify")]
    assert "graph TD" in info["mermaid"]
    assert "classify --> retrieve" in info["mermaid"]


def test_compiled_graph_exposes_invoke_and_stream() -> None:
    graph = build_minimal_graph()
    assert hasattr(graph, "ainvoke")
    assert hasattr(graph, "astream")
    assert type(graph).__name__ == "CompiledStateGraph"
