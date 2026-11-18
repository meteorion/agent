# file: src/servicedesk/graph/approval.py
"""人工审批门禁：用 interrupt() 挂起，用 Command(resume=...) 恢复。

三件事必须一起成立：

1. **interrupt()** 把图停在当前节点，并把"审批单"交给人类
2. **checkpointer** 让挂起期间的状态活下来（进程可以退出）
3. **Command(resume=...)** 让人做完决定后从同一位置继续

⚠️ 最重要的一条（§5.2 实测）：**被 resume 的节点会从头重跑**。
   所以 `interrupt()` 之前不能放副作用——否则人类点一次"批准"，通知就发两次。
"""

from __future__ import annotations

import operator
import time
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from servicedesk.graph.checkpoint import open_sqlite_checkpointer

# ------------------------------- State -------------------------------


class ApprovalState(TypedDict, total=False):
    ticket_id: str
    intent: str
    draft: str
    risk: str               # high / medium / low
    expires_at: float       # 审批截止（绝对时间戳）
    decision: str           # approve / reject / edit / expired
    decided_by: str
    final: str
    trace: Annotated[list[str], operator.add]


DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISION_EDIT = "edit"
DECISION_EXPIRED = "expired"


# ------------------------------- 节点 -------------------------------


def prepare(state: ApprovalState) -> dict[str, Any]:
    """准备待审批的答复。这是 interrupt 之前的节点，**重跑也只跑这一次**。"""
    draft = state.get("draft") or "根据资料：认证失败请先在自助门户重置密码。[C1]"
    expires_at = state.get("expires_at") or (time.time() + 300)
    return {
        "draft": draft,
        "expires_at": expires_at,
        "trace": ["prepare"],
    }


def human_gate(state: ApprovalState) -> dict[str, Any]:
    """审批门禁：挂起等人类决定。

    ⚠️ 这个节点在恢复时会**从头再执行一遍**。所以：
       - 不要在 interrupt() 之前做任何有副作用的操作
       - 需要"进入次数"这类计数，放到 State 之外单独记（仅用于观测）
    """
    payload = {
        "type": "approval_request",
        "ticket_id": state.get("ticket_id"),
        "draft": state.get("draft"),
        "risk": state.get("risk", "high"),
        "expires_at": state.get("expires_at"),
        "ask": "批准这条答复吗？可回复 approve / reject，或 {action: edit, text: 新答复}",
    }

    # 挂起：返回值就是 resume 时传进来的东西
    answer = interrupt(payload)

    if isinstance(answer, dict):
        action = str(answer.get("action") or "").strip().lower()
        edited = answer.get("text")
        if action == DECISION_EDIT and edited:
            return {"decision": DECISION_EDIT, "draft": str(edited), "trace": ["gate:edit"]}
        decision = action or DECISION_REJECT
        return {"decision": decision, "decided_by": str(answer.get("by") or ""), "trace": [f"gate:{decision}"]}

    decision = str(answer or "").strip().lower()
    return {"decision": decision or DECISION_REJECT, "trace": [f"gate:{decision or 'reject'}"]}


def route_decision(state: ApprovalState) -> Literal["deliver", "cancelled", "expired"]:
    """三道出口：批准 → 交付；拒绝 → 取消；超时 → 过期（**即使人说批准**）。"""
    if time.time() > float(state.get("expires_at") or 0):
        return "expired"
    decision = state.get("decision")
    if decision in (DECISION_APPROVE, DECISION_EDIT):
        return "deliver"
    return "cancelled"


def deliver(state: ApprovalState) -> dict[str, Any]:
    return {
        "final": f"已交付：{state.get('draft')}",
        "trace": ["deliver"],
    }


def cancelled(state: ApprovalState) -> dict[str, Any]:
    return {
        "final": "审批未通过，答复已作废，已转人工处理。",
        "trace": ["cancelled"],
    }


def expired(state: ApprovalState) -> dict[str, Any]:
    return {
        "final": "审批超时，答复已作废。请重新发起或联系值班人员。",
        "trace": ["expired"],
    }


# ------------------------------- 组装 -------------------------------


def build_approval_graph(checkpointer: Any = None):
    builder = StateGraph(ApprovalState)
    builder.add_node("prepare", prepare)
    builder.add_node("human_gate", human_gate)
    builder.add_node("deliver", deliver)
    builder.add_node("cancelled", cancelled)
    builder.add_node("expired", expired)

    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "human_gate")
    builder.add_conditional_edges(
        "human_gate",
        route_decision,
        {"deliver": "deliver", "cancelled": "cancelled", "expired": "expired"},
    )
    builder.add_edge("deliver", END)
    builder.add_edge("cancelled", END)
    builder.add_edge("expired", END)

    return builder.compile(checkpointer=checkpointer)


# ------------------------------- 对外接口 -------------------------------


def open_approval_checkpointer(path: str | Path):
    """审批场景用同一套 SQLite checkpointer（挂起期间状态就存在这里）。"""
    return open_sqlite_checkpointer(path)


def thread_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


async def start(
    graph: Any,
    thread_id: str,
    *,
    ticket_id: str,
    draft: str = "",
    risk: str = "high",
    timeout_s: float = 300,
) -> dict[str, Any]:
    """发起审批：跑到 interrupt 处停下，返回审批单。"""
    out = await graph.ainvoke(
        {
            "ticket_id": ticket_id,
            "draft": draft,
            "risk": risk,
            "expires_at": time.time() + timeout_s,
            "trace": [],
        },
        config=thread_config(thread_id),
    )
    return {"result": out, "pending": await pending(graph, thread_id)}


async def pending(graph: Any, thread_id: str) -> dict[str, Any] | None:
    """当前是否有等待中的审批；有就返回审批单。"""
    state = await graph.aget_state(thread_config(thread_id))
    if not state.interrupts:
        return None
    return {
        "payload": state.interrupts[0].value,
        "interrupt_id": getattr(state.interrupts[0], "id", None),
        "next": tuple(state.next),
        "trace": list(state.values.get("trace", [])),
    }


async def decide(graph: Any, thread_id: str, decision: Any) -> dict[str, Any]:
    """人类做完决定：用 Command(resume=...) 从挂起点继续。"""
    out = await graph.ainvoke(Command(resume=decision), config=thread_config(thread_id))
    return {
        "final": out.get("final"),
        "decision": out.get("decision"),
        "trace": out.get("trace", []),
    }
