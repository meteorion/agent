# file: src/servicedesk/graph/composite.py
"""子图拆分：把大图拆成可复用的小图，再合成总图。

三种接入方式（本节实现前两种，第三种只说明）：

    ① 编译好的子图**直接当节点**：`builder.add_node("retrieval", subgraph)`
       —— 最简单，state 直接透传；子图里的 interrupt 会冒泡到父图

    ② 用**包装函数**调用子图：`async def node(state): return await subgraph.ainvoke(state)`
       —— 需要做 state 映射、或想在调用前后加逻辑时用

    ③ 用 input/output schema 做显式映射（新版支持）
       —— 子图与父图的字段名不同、且不想在包装函数里手写映射时用

本节的子图与父图**共享同一个 State schema**，所以 ① 就够了。
"""

from __future__ import annotations

import operator
import time
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

# ------------------------------- State -------------------------------


class TicketFlowState(TypedDict, total=False):
    """父图与两个子图共享的状态。"""

    # 输入
    ticket_id: str
    title: str
    description: str

    # 检索子图产出
    intent: str
    docs: list[str]
    draft: str
    error: str | None

    # 审批子图产出
    risk: str
    expires_at: float
    decision: str
    final: str

    # 观测
    trace: Annotated[list[str], operator.add]
    stages: Annotated[list[str], operator.add]     # 走过的子图


_DOCS: dict[str, list[str]] = {
    "vpn": ["认证失败通常有三类原因：密码过期、动态口令偏差、账号锁定。",
            "处理顺序：自助门户重置密码 → 校准手机时间。"],
    "email": ["单封邮件附件上限 25 MB，超出会被网关退回。"],
}


# --------------------------- 子图 A：检索 ---------------------------


def classify(state: TicketFlowState) -> dict[str, Any]:
    text = f"{state.get('title', '')} {state.get('description', '')}".lower()
    if any(word in text for word in ("vpn", "认证", "隧道")):
        intent = "vpn"
    elif any(word in text for word in ("邮箱", "邮件", "附件")):
        intent = "email"
    else:
        intent = "other"
    return {"intent": intent, "trace": ["classify"], "stages": ["retrieval"]}


def fetch(state: TicketFlowState) -> dict[str, Any]:
    intent = state.get("intent", "other")
    return {"docs": list(_DOCS.get(intent, [])), "trace": ["fetch"]}


def compose_draft(state: TicketFlowState) -> dict[str, Any]:
    docs = state.get("docs") or []
    draft = "根据资料：" + " ".join(docs) + "[C1]" if docs else ""
    return {"draft": draft, "trace": ["draft"]}


def ask_more(state: TicketFlowState) -> dict[str, Any]:
    return {"draft": "", "error": "资料不足，需要用户补充信息", "trace": ["ask_more"]}


def route_after_fetch(state: TicketFlowState) -> Literal["compose", "ask"]:
    return "compose" if state.get("docs") else "ask"


def build_retrieval_subgraph():
    """子图 A：识别意图 → 检索 → 拼草稿（或追问）。"""
    builder = StateGraph(TicketFlowState)
    builder.add_node("classify", classify)
    builder.add_node("fetch", fetch)
    builder.add_node("compose", compose_draft)
    builder.add_node("ask_more", ask_more)

    builder.add_edge(START, "classify")
    builder.add_edge("classify", "fetch")
    builder.add_conditional_edges("fetch", route_after_fetch, {"compose": "compose", "ask": "ask_more"})
    builder.add_edge("compose", END)
    builder.add_edge("ask_more", END)
    return builder.compile()


# --------------------------- 子图 B：审批 ---------------------------


def prepare_approval(state: TicketFlowState) -> dict[str, Any]:
    return {
        "risk": state.get("risk") or ("high" if state.get("intent") == "vpn" else "low"),
        "expires_at": state.get("expires_at") or (time.time() + 300),
        "trace": ["prepare_approval"],
        "stages": ["approval"],
    }


def gate(state: TicketFlowState) -> dict[str, Any]:
    """挂起等人审批。子图里的 interrupt 会冒泡到父图。"""
    if not state.get("draft"):
        # 没有草稿就没有可审批的东西，直接短路（不算挂起）
        return {"decision": "skipped", "trace": ["gate:skipped"]}

    answer = interrupt(
        {
            "type": "approval_request",
            "ticket_id": state.get("ticket_id"),
            "draft": state.get("draft"),
            "risk": state.get("risk"),
            "expires_at": state.get("expires_at"),
            "ask": "批准这条答复吗？",
        }
    )
    decision = str(answer or "reject").strip().lower()
    return {"decision": decision, "trace": [f"gate:{decision}"]}


def route_decision(state: TicketFlowState) -> Literal["deliver", "cancelled", "expired", "skipped"]:
    if state.get("decision") == "skipped":
        return "skipped"
    if time.time() > float(state.get("expires_at") or 0):
        return "expired"
    return "deliver" if state.get("decision") in ("approve", "edit") else "cancelled"


def deliver(state: TicketFlowState) -> dict[str, Any]:
    return {"final": f"已交付：{state.get('draft')}", "trace": ["deliver"]}


def cancelled(state: TicketFlowState) -> dict[str, Any]:
    return {"final": "审批未通过，已转人工。", "trace": ["cancelled"]}


def expired(state: TicketFlowState) -> dict[str, Any]:
    return {"final": "审批超时，答复已作废。", "trace": ["expired"]}


def skipped(state: TicketFlowState) -> dict[str, Any]:
    return {"final": "资料不足，已请用户补充信息。", "trace": ["skipped"]}


def build_approval_subgraph():
    """子图 B：准备 → 挂起审批 → 三路出口。"""
    builder = StateGraph(TicketFlowState)
    builder.add_node("prepare_approval", prepare_approval)
    builder.add_node("gate", gate)
    builder.add_node("deliver", deliver)
    builder.add_node("cancelled", cancelled)
    builder.add_node("expired", expired)
    builder.add_node("skipped", skipped)

    builder.add_edge(START, "prepare_approval")
    builder.add_edge("prepare_approval", "gate")
    builder.add_conditional_edges(
        "gate",
        route_decision,
        {"deliver": "deliver", "cancelled": "cancelled", "expired": "expired", "skipped": "skipped"},
    )
    for node in ("deliver", "cancelled", "expired", "skipped"):
        builder.add_edge(node, END)
    return builder.compile()


# --------------------------- 合成：方式 ① ---------------------------


def build_composite_graph(checkpointer: Any = None):
    """父图：检索子图 → 审批子图。子图直接当节点用。"""
    builder = StateGraph(TicketFlowState)
    builder.add_node("retrieval", build_retrieval_subgraph())
    builder.add_node("approval", build_approval_subgraph())

    builder.add_edge(START, "retrieval")
    builder.add_edge("retrieval", "approval")
    builder.add_edge("approval", END)
    return builder.compile(checkpointer=checkpointer)


# --------------------------- 合成：方式 ② ---------------------------


def build_composite_via_callable(checkpointer: Any = None):
    """父图：检索用**包装函数**（做 state 映射），审批仍用子图直接当节点。

    为什么审批不也用包装函数？因为子图里有 `interrupt`——包装函数里调
    `subgraph.ainvoke()` 会**另起一次执行**，父图的 checkpointer 上下文传不进去，
    挂起后就无处恢复。带 interrupt 的子图**必须**直接当节点用。
    """
    retrieval = build_retrieval_subgraph()

    async def retrieval_node(state: TicketFlowState) -> dict[str, Any]:
        result = await retrieval.ainvoke(state)
        # 想改名、想裁剪字段，都在这里做
        return {"intent": result.get("intent"), "docs": result.get("docs"),
                "draft": result.get("draft"), "error": result.get("error"),
                "trace": ["retrieval(callable)"], "stages": ["retrieval"]}

    builder = StateGraph(TicketFlowState)
    builder.add_node("retrieval", retrieval_node)
    builder.add_node("approval", build_approval_subgraph())
    builder.add_edge(START, "retrieval")
    builder.add_edge("retrieval", "approval")
    builder.add_edge("approval", END)
    return builder.compile(checkpointer=checkpointer)


# ------------------------------- 观测 -------------------------------


def describe_nested(graph: Any) -> dict[str, Any]:
    """把嵌套结构摊平：父图有哪几个节点，子图内部有哪些节点。"""
    plain = graph.get_graph()
    info: dict[str, Any] = {
        "parent_nodes": [name for name in plain.nodes if not name.startswith("__")],
        "parent_edges": [
            (edge.source, edge.target)
            for edge in plain.edges
            if not edge.source.startswith("__") and not edge.target.startswith("__")
        ],
    }
    try:
        xray = graph.get_graph(xray=True)
        info["nested_nodes"] = [
            name for name in xray.nodes if ":" in name or not name.startswith("__")
        ]
    except Exception as exc:  # noqa: BLE001 —— xray 是可选能力，拿不到就说明一下
        info["nested_error"] = str(exc)
    return info


# ------------------------------- 对外接口 -------------------------------


def thread_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


async def run_flow(
    graph: Any, thread_id: str, *, ticket_id: str, title: str, description: str
) -> dict[str, Any]:
    """跑到挂起点（或直接跑完）。"""
    return await graph.ainvoke(
        {"ticket_id": ticket_id, "title": title, "description": description, "trace": []},
        config=thread_config(thread_id),
    )


async def pending_interrupt(graph: Any, thread_id: str) -> dict[str, Any] | None:
    state = await graph.aget_state(thread_config(thread_id))
    if not state.interrupts:
        return None
    return {"payload": state.interrupts[0].value, "next": tuple(state.next)}


async def resume_flow(graph: Any, thread_id: str, decision: Any) -> dict[str, Any]:
    return await graph.ainvoke(Command(resume=decision), config=thread_config(thread_id))
