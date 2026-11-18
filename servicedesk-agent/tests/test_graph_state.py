# file: tests/test_graph_state.py
"""3.2 的断言：默认覆盖、reducer 累加、add_messages 的 id 语义、派生字段。"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from servicedesk.graph.state import (
    TicketState,
    derive_needs_approval,
    describe_state_schema,
    merge_dicts,
)


def content_of(message: Any) -> str:
    return str(getattr(message, "content", None) or message.get("content", ""))


def role_of(message: Any) -> str:
    return str(getattr(message, "type", None) or message.get("role", "?"))


# ------------------------------- reducer -------------------------------


def test_merge_dicts_right_wins() -> None:
    assert merge_dicts({"a": 1, "b": 2}, {"b": 3, "c": 4}) == {"a": 1, "b": 3, "c": 4}


def test_merge_dicts_handles_none() -> None:
    """LangGraph 在字段首次写入时会传 None。"""
    assert merge_dicts(None, {"a": 1}) == {"a": 1}
    assert merge_dicts({"a": 1}, None) == {"a": 1}
    assert merge_dicts(None, None) == {}


def test_merge_dicts_does_not_mutate_inputs() -> None:
    left = {"a": 1}
    merge_dicts(left, {"b": 2})
    assert left == {"a": 1}


# ------------------------------- 派生字段 -------------------------------


@pytest.mark.parametrize(
    ("intent", "draft", "expected"),
    [
        ("vpn", "先重置密码", True),
        ("vpn", "只是查询", False),
        ("email", "先重置密码", False),
        (None, None, False),
        ("vpn", None, False),
    ],
)
def test_derive_needs_approval(intent: str | None, draft: str | None, expected: bool) -> None:
    assert derive_needs_approval(intent, draft) is expected


def test_schema_describes_write_semantics() -> None:
    table = describe_state_schema()
    assert "累加" in table["messages"]
    assert "累加" in table["trace"]
    assert "累加" in table["sources"]
    assert "覆盖" in table["intent"]
    assert "不存" in table["needs_approval"]


def test_ticket_state_accepts_partial_input() -> None:
    """total=False 让 State 可以"逐步补全"。"""
    partial: TicketState = {"title": "VPN 连不上"}
    assert partial["title"] == "VPN 连不上"


# ------------------------------ 图里的行为 ------------------------------


class PlainState(TypedDict):
    trace: list[str]


class ReducerState(TypedDict):
    trace: Annotated[list[str], operator.add]


def _two_step(label_a: str, label_b: str):
    def step_a(state):  # type: ignore[no-untyped-def]
        return {"trace": [label_a]}

    def step_b(state):  # type: ignore[no-untyped-def]
        return {"trace": [label_b]}

    return step_a, step_b


async def _run(schema, initial: dict) -> dict:  # type: ignore[no-untyped-def]
    step_a, step_b = _two_step("a", "b")
    builder = StateGraph(schema)
    builder.add_node("a", step_a)
    builder.add_node("b", step_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    return await builder.compile().ainvoke(initial)


async def test_default_behaviour_overwrites() -> None:
    """没有 reducer 时，后写的整个覆盖先写的。"""
    out = await _run(PlainState, {"trace": []})
    assert out["trace"] == ["b"]


async def test_operator_add_accumulates() -> None:
    out = await _run(ReducerState, {"trace": []})
    assert out["trace"] == ["a", "b"]


class MsgPlain(TypedDict):
    messages: list[dict[str, Any]]


class MsgReducer(TypedDict):
    messages: Annotated[list[dict[str, Any]], add_messages]


async def test_messages_without_reducer_lose_history() -> None:
    def user(state):  # type: ignore[no-untyped-def]
        return {"messages": [{"role": "user", "content": "我想重置密码"}]}

    def assist(state):  # type: ignore[no-untyped-def]
        return {"messages": [{"role": "assistant", "content": "好的"}]}

    builder = StateGraph(MsgPlain)
    builder.add_node("user", user)
    builder.add_node("assist", assist)
    builder.add_edge(START, "user")
    builder.add_edge("user", "assist")
    builder.add_edge("assist", END)
    out = await builder.compile().ainvoke({"messages": []})

    assert len(out["messages"]) == 1
    assert role_of(out["messages"][0]) == "assistant"


async def test_add_messages_keeps_both_roles() -> None:
    def user(state):  # type: ignore[no-untyped-def]
        return {"messages": [{"role": "user", "content": "我想重置密码"}]}

    def assist(state):  # type: ignore[no-untyped-def]
        return {"messages": [{"role": "assistant", "content": "好的"}]}

    builder = StateGraph(MsgReducer)
    builder.add_node("user", user)
    builder.add_node("assist", assist)
    builder.add_edge(START, "user")
    builder.add_edge("user", "assist")
    builder.add_edge("assist", END)
    out = await builder.compile().ainvoke({"messages": []})

    assert [role_of(m) for m in out["messages"]] == ["human", "ai"]
    assert content_of(out["messages"][0]) == "我想重置密码"


async def test_add_messages_replaces_same_id() -> None:
    """同 id 的消息是「替换」而不是「重复追加」——润色回答要靠这个。"""

    def first(state):  # type: ignore[no-untyped-def]
        return {"messages": [{"role": "assistant", "content": "草稿 v1", "id": "m1"}]}

    def second(state):  # type: ignore[no-untyped-def]
        return {"messages": [{"role": "assistant", "content": "草稿 v2", "id": "m1"}]}

    builder = StateGraph(MsgReducer)
    builder.add_node("first", first)
    builder.add_node("second", second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    out = await builder.compile().ainvoke({"messages": []})

    assert len(out["messages"]) == 1
    assert content_of(out["messages"][0]) == "草稿 v2"


class DictState(TypedDict):
    sources: Annotated[dict[str, Any], merge_dicts]


async def test_custom_reducer_merges_dicts() -> None:
    def r1(state):  # type: ignore[no-untyped-def]
        return {"sources": {"c1": "vpn-guide"}}

    def r2(state):  # type: ignore[no-untyped-def]
        return {"sources": {"c1": "vpn-guide-v2", "c2": "email-faq"}}

    builder = StateGraph(DictState)
    builder.add_node("r1", r1)
    builder.add_node("r2", r2)
    builder.add_edge(START, "r1")
    builder.add_edge("r1", "r2")
    builder.add_edge("r2", END)
    out = await builder.compile().ainvoke({"sources": {}})

    assert out["sources"] == {"c1": "vpn-guide-v2", "c2": "email-faq"}
