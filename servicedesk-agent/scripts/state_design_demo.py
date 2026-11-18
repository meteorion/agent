# file: scripts/state_design_demo.py
"""3.2 的场景：默认覆盖 vs reducer，以及"可重算的字段别存"。

用法：
    uv run python -m scripts.state_design_demo
"""

from __future__ import annotations

import asyncio
import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from servicedesk.graph.state import (
    TicketState,
    derive_needs_approval,
    describe_state_schema,
    merge_dicts,
)


def role_of(message: Any) -> str:
    """add_messages 会把 dict 转成 BaseMessage，所以两种形状都要能读。"""
    return str(getattr(message, "type", None) or message.get("role", "?"))


def content_of(message: Any) -> str:
    return str(getattr(message, "content", None) or message.get("content", ""))


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# --------------------------- 实验用最小 schema ---------------------------


class PlainState(TypedDict):
    trace: list[str]


class ReducerState(TypedDict):
    trace: Annotated[list[str], operator.add]


class MsgPlain(TypedDict):
    messages: list[dict[str, Any]]


class MsgReducer(TypedDict):
    messages: Annotated[list[dict[str, Any]], add_messages]


def make_step(label: str):
    def step(state):        # type: ignore[no-untyped-def]
        return {"trace": [label]}
    return step


async def run_two_nodes(schema, initial):  # type: ignore[no-untyped-def]
    builder = StateGraph(schema)
    builder.add_node("a", make_step("node-a"))
    builder.add_node("b", make_step("node-b"))
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    return await builder.compile().ainvoke(initial)


async def demo_append() -> None:
    header("第 1 步：默认覆盖 —— 两次写入只剩最后一次")
    plain = await run_two_nodes(PlainState, {"trace": []})
    with_reducer = await run_two_nodes(ReducerState, {"trace": []})

    print(f"  无 reducer : trace = {plain['trace']}      ← node-a 的痕迹没了")
    print(f"  有 reducer : trace = {with_reducer['trace']}")
    print()
    print("  >>> 默认行为是『后来者胜』。两个节点都写 trace，前一个被整个覆盖。")
    print("      这不是 bug，是设计：对 intent 这种字段，我们**就是**要后来者胜。")


async def demo_messages() -> None:
    header("第 2 步：messages —— 为什么必须是 add_messages")

    def say(role: str, content: str, msg_id: str | None = None):
        message: dict[str, Any] = {"role": role, "content": content}
        if msg_id:
            message["id"] = msg_id
        return message

    async def plain_flow() -> dict:
        builder = StateGraph(MsgPlain)
        builder.add_node("user", lambda s: {"messages": [say("user", "我想重置密码")]})
        builder.add_node("assist", lambda s: {"messages": [say("assistant", "好的")]})
        builder.add_edge(START, "user")
        builder.add_edge("user", "assist")
        builder.add_edge("assist", END)
        return await builder.compile().ainvoke({"messages": []})

    async def reducer_flow() -> dict:
        builder = StateGraph(MsgReducer)
        builder.add_node("user", lambda s: {"messages": [say("user", "我想重置密码")]})
        builder.add_node("assist", lambda s: {"messages": [say("assistant", "好的")]})
        builder.add_edge(START, "user")
        builder.add_edge("user", "assist")
        builder.add_edge("assist", END)
        return await builder.compile().ainvoke({"messages": []})

    plain = await plain_flow()
    reduced = await reducer_flow()
    print(f"  无 reducer : {len(plain['messages'])} 条 → "
          f"{[role_of(m) for m in plain['messages']]}")
    print(f"  有 reducer : {len(reduced['messages'])} 条 → "
          f"{[role_of(m) for m in reduced['messages']]}")
    print()
    print("  >>> 无 reducer 时，第二轮把第一轮的用户消息**覆盖掉了**。")
    print("      模型只看得到『好的』，根本不知道自己在回应什么。")

    # add_messages 的额外能力：同 id 替换，而不是重复追加
    class Updatable(TypedDict):
        messages: Annotated[list[dict[str, Any]], add_messages]

    builder = StateGraph(Updatable)
    builder.add_node("first", lambda s: {"messages": [say("assistant", "草稿 v1", "m1")]})
    builder.add_node("second", lambda s: {"messages": [say("assistant", "草稿 v2", "m1")]})
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    out = await builder.compile().ainvoke({"messages": []})

    print(f"  同 id 更新 : {len(out['messages'])} 条 → "
          f"{[content_of(m) for m in out['messages']]}")
    print("  >>> 这就是『润色回答』这类场景要的语义：改同一条，而不是再加一条。")


async def demo_custom_reducer() -> None:
    header("第 3 步：自定义 reducer —— 字典合并")

    class S(TypedDict):
        sources: Annotated[dict[str, Any], merge_dicts]

    builder = StateGraph(S)
    builder.add_node("r1", lambda s: {"sources": {"c1": "vpn-guide"}})
    builder.add_node("r2", lambda s: {"sources": {"c2": "email-faq", "c1": "vpn-guide-v2"}})
    builder.add_edge(START, "r1")
    builder.add_edge("r1", "r2")
    builder.add_edge("r2", END)
    out = await builder.compile().ainvoke({"sources": {}})

    print(f"  合并结果: {out['sources']}")
    print("  >>> 同名键 c1 被右边覆盖（v2），新键 c2 被加入。")
    print("      如果用默认覆盖，r1 收集的 c1 会整个消失。")


def demo_derived_field() -> None:
    header("第 4 步：可重算的字段，别存")
    print("  情景：把 needs_approval 存进 State，然后改了 intent")
    stored = {"intent": "vpn", "draft": "先重置密码", "needs_approval": True}
    print(f"    改之前: {stored}")
    stored["intent"] = "email"          # 只改了 intent，忘了同步 needs_approval
    print(f"    改了 intent 之后: {stored}")
    print(f"    自相矛盾吗: needs_approval={stored['needs_approval']} 但 "
          f"derive()={derive_needs_approval(stored['intent'], stored['draft'])}")
    print()
    print("  如果只存原料（intent + draft），每次都现算：")
    intents = [("vpn", "先重置密码"), ("email", "先重置密码"), ("vpn", "只是查询")]
    for intent, text in intents:
        print(f"    intent={intent:<6} draft={text:<8} → "
              f"needs_approval={derive_needs_approval(intent, text)}")
    print("  >>> 永远不会出现两份互相打架的真相。")


def demo_schema_table() -> None:
    header("第 5 步：把 State 的写入语义列出来（写进设计文档）")
    for field, semantics in describe_state_schema().items():
        print(f"  {field:<16} {semantics}")


async def main() -> int:
    await demo_append()
    await demo_messages()
    await demo_custom_reducer()
    demo_derived_field()
    demo_schema_table()

    header("小结")
    print("  1. 默认行为是覆盖：对 intent 正确，对 messages/trace 灾难。")
    print("  2. messages 必须用 add_messages：它还会按 id 替换而不是重复追加。")
    print("  3. 字典字段要自己写 reducer（merge_dicts），否则收集来源会丢。")
    print("  4. 可重算的字段不要存：存了两份真相，就有不一致的风险。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
