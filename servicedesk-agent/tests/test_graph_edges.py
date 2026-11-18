# file: tests/test_graph_edges.py
"""3.3 的断言：三路分支、重试循环、刹车、错误兜底、Command 跳转。"""

from __future__ import annotations

import pytest
from langgraph.errors import GraphRecursionError

from servicedesk.graph.edges import (
    RouteState,
    build_branching_graph,
    build_command_graph,
    classify,
    describe_routes,
    route_after_fetch,
    route_by_intent,
)


def state(**kwargs) -> RouteState:
    base: RouteState = {"trace": []}
    base.update(kwargs)  # type: ignore[typeddict-item]
    return base


# -------------------------------- 路由 --------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [("VPN 连不上", "vpn"), ("邮件发不出", "email"), ("打印机坏了", "other")],
)
def test_route_by_intent(title: str, expected: str) -> None:
    assert route_by_intent(classify(state(title=title))) == expected


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"docs": ["x"], "attempts": 1}, "answer"),
        ({"docs": [], "error": "超时", "attempts": 1}, "fallback"),
        ({"docs": [], "attempts": 1, "max_attempts": 3}, "retry"),
        ({"docs": [], "attempts": 3, "max_attempts": 3}, "clarify"),
        ({"docs": [], "attempts": 0, "max_attempts": 0}, "clarify"),
    ],
)
def test_route_after_fetch(fields: dict, expected: str) -> None:
    assert route_after_fetch(state(**fields)) == expected


def test_error_takes_priority_over_docs() -> None:
    """即使拿到了资料，只要有 error 也先去兜底——错误不能被静默吞掉。"""
    assert route_after_fetch(state(docs=["x"], error="boom", attempts=1)) == "fallback"


# ------------------------------ 分支与循环 ------------------------------


async def test_three_way_branch() -> None:
    graph = build_branching_graph(empty_rounds=0)
    cases = {
        "VPN 认证失败": "fetch_vpn",
        "邮件附件太大": "fetch_email",
        "打印机坏了": None,   # other → clarify
    }
    for title, expected_fetch in cases.items():
        out = await graph.ainvoke({"title": title, "trace": [], "max_attempts": 3})
        if expected_fetch:
            assert expected_fetch in out["trace"]
            assert out["trace"][-1] == "answer"
        else:
            assert "clarify" in out["trace"]


async def test_retry_loops_back_once_then_succeeds() -> None:
    graph = build_branching_graph(empty_rounds=1, max_attempts=3)
    out = await graph.ainvoke(
        {"title": "VPN 认证失败", "trace": [], "max_attempts": 3}
    )

    assert out["trace"] == ["classify", "fetch_vpn", "fetch_vpn", "answer"]
    assert out["attempts"] == 2


async def test_brake_stops_after_max_attempts() -> None:
    graph = build_branching_graph(empty_rounds=99, max_attempts=3)
    out = await graph.ainvoke(
        {"title": "VPN 认证失败", "trace": [], "max_attempts": 3}
    )

    assert out["attempts"] == 3
    assert out["trace"] == ["classify", "fetch_vpn", "fetch_vpn", "fetch_vpn", "clarify"]


async def test_without_brake_recursion_limit_raises() -> None:
    """没有业务刹车时，只能靠 recursion_limit 兜住。"""
    graph = build_branching_graph(empty_rounds=10**9, max_attempts=10**9)
    with pytest.raises(GraphRecursionError):
        await graph.ainvoke(
            {"title": "VPN 认证失败", "trace": [], "max_attempts": 10**9},
            {"recursion_limit": 10},
        )


# ------------------------------ 错误边 ------------------------------


async def test_downstream_error_routes_to_fallback() -> None:
    graph = build_branching_graph(empty_rounds=0)
    out = await graph.ainvoke(
        {"title": "VPN 认证失败", "description": "接口超时", "trace": [], "max_attempts": 3}
    )

    assert out["trace"] == ["classify", "fetch_vpn", "fallback"]
    assert "查询暂时失败" in out["answer"]
    assert "超时" in out["answer"]


# ------------------------------ Command ------------------------------


async def test_command_graph_retries_then_answers() -> None:
    graph = build_command_graph(empty_rounds=1)
    out = await graph.ainvoke(
        {"title": "VPN 认证失败", "trace": [], "max_attempts": 3}
    )
    assert out["trace"] == ["classify", "fetch_vpn", "fetch_vpn", "answer"]


async def test_command_graph_respects_max_attempts() -> None:
    graph = build_command_graph(empty_rounds=99)
    out = await graph.ainvoke(
        {"title": "邮件附件太大", "trace": [], "max_attempts": 2}
    )
    assert out["attempts"] == 2
    assert out["trace"][-1] == "clarify"


async def test_command_graph_error_goes_to_fallback() -> None:
    graph = build_command_graph(empty_rounds=0)
    out = await graph.ainvoke(
        {"title": "VPN 认证失败", "description": "接口超时", "trace": [], "max_attempts": 3}
    )
    assert out["trace"][-1] == "fallback"


# ------------------------------ 结构导出 ------------------------------


def test_describe_routes_shows_conditional_labels() -> None:
    rows = describe_routes(build_branching_graph(empty_rounds=1))
    labels = {(source, target): label for source, target, label in rows}

    assert labels[("classify", "fetch_vpn")] == "vpn"
    assert labels[("classify", "fetch_email")] == "email"
    assert labels[("classify", "clarify")] == "other"
    assert labels[("fetch_vpn", "fetch_vpn")] == "retry"
