# file: src/servicedesk/graph/minimal.py
"""第一个最小状态图：三个节点、两条边。

这一节只做一件事——**把"控制流"从 if-else 搬到图上**，让你看清三件事：

    State  —— 一份共享的、可被每步读写的状态（不用再穿透参数）
    Node   —— 一个纯函数，读 State、返回"要更新的字段"
    Edge   —— 明确的执行顺序（想分支时再加条件边，见 3.3）

刻意**不用** LLM：这节要观察的是控制流，不是模型能力。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

# ------------------------------- State -------------------------------


class TicketState(TypedDict, total=False):
    """图里所有节点共享的状态。

    用 `total=False` 是因为它天然是"逐步补全"的：
    刚开始只有 title，跑到后面才有 intent/docs/draft。
    """

    # 输入
    title: str
    description: str

    # 中间产物
    intent: str          # vpn | email | other
    docs: list[str]

    # 输出
    draft: str
    needs_approval: bool

    # 观察用：每步追加，方便看清"到底走了哪条路"
    trace: list[str]


# ------------------------------- Nodes -------------------------------

_VPN_WORDS = ("vpn", "认证", "隧道", "掉线")
_EMAIL_WORDS = ("邮箱", "邮件", "附件", "垃圾邮件")

_DOCS: dict[str, list[str]] = {
    "vpn": [
        "认证失败通常有三类原因：密码过期、动态口令时间偏差、账号被锁定。",
        "处理顺序：自助门户重置密码 → 校准手机时间 → 提交工单申请解锁。",
    ],
    "email": [
        "单封邮件附件上限 25 MB，超出会被网关退回。",
        "大文件请用企业网盘分享链接，不要拆成多封邮件。",
    ],
    "other": [],
}


def classify(state: TicketState) -> dict[str, Any]:
    """节点 1：判断意图。返回**要更新的字段**，不是整个 state。"""
    text = f"{state.get('title', '')} {state.get('description', '')}".lower()

    if any(word in text for word in _VPN_WORDS):
        intent = "vpn"
    elif any(word in text for word in _EMAIL_WORDS):
        intent = "email"
    else:
        intent = "other"

    return {
        "intent": intent,
        "trace": [*state.get("trace", []), "classify"],
    }


def retrieve(state: TicketState) -> dict[str, Any]:
    """节点 2：按意图取资料。"""
    intent = state.get("intent", "other")
    return {
        "docs": list(_DOCS.get(intent, [])),
        "trace": [*state.get("trace", []), "retrieve"],
    }


def draft(state: TicketState) -> dict[str, Any]:
    """节点 3：拼出答复，并判断要不要人工审批。"""
    docs = state.get("docs") or []
    intent = state.get("intent", "other")

    if not docs:
        draft = "资料中没有相关信息，建议提交 IT 工单或转人工。"
    else:
        draft = "根据资料：" + " ".join(docs) + "[C1]"

    # 涉及账号操作的结论必须人工确认（3.5 会用 interrupt 真正挂起）
    needs_approval = intent == "vpn" and "重置" in draft

    return {
        "draft": draft,
        "needs_approval": needs_approval,
        "trace": [*state.get("trace", []), "draft"],
    }


# ------------------------------- 组装 -------------------------------


def build_minimal_graph():
    """State + Node + Edge → compile() → 可执行的图。"""
    builder = StateGraph(TicketState)
    builder.add_node("classify", classify)
    builder.add_node("retrieve", retrieve)
    builder.add_node("draft", draft)

    builder.add_edge(START, "classify")
    builder.add_edge("classify", "retrieve")
    builder.add_edge("retrieve", "draft")
    builder.add_edge("draft", END)

    return builder.compile()


def describe_graph(graph: Any) -> dict[str, Any]:
    """把编译后的图结构摊平，方便对照"你画的那张图"。"""
    drawable = graph.get_graph()
    return {
        "nodes": [name for name in drawable.nodes if not name.startswith("__")],
        "edges": [
            (edge.source, edge.target)
            for edge in drawable.edges
            if not edge.source.startswith("__") and not edge.target.startswith("__")
        ],
        "entry_edges": [
            (edge.source, edge.target) for edge in drawable.edges if edge.source.startswith("__")
        ],
        "mermaid": drawable.draw_mermaid(),
    }
