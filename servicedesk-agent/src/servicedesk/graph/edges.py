# file: src/servicedesk/graph/edges.py
"""条件边与循环：让图会「回头看」。

三个机制，各自解决一类问题：

    add_conditional_edges  —— 按 State 决定下一步去哪（三路分支 / 重试）
    Command(goto=...)      —— 节点自己决定去哪（不用单独的 router 函数）
    max_attempts + 兜底节点 —— 循环必须有的刹车，否则就是死循环

**错误边**在 LangGraph 里没有"自动捕获异常"的边：惯例是**节点内 try/except → 写 state.error
→ 条件边把 error 导到兜底节点**。本节就是这么做的。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

# ------------------------------- State -------------------------------


class RouteState(TypedDict, total=False):
    # 输入
    title: str
    description: str
    max_attempts: int

    # 中间
    intent: str
    docs: list[str]
    attempts: int
    error: str | None

    # 输出
    answer: str
    route: str            # 最近一次路由决策，便于观察

    # 轨迹（3.2 的 reducer）
    trace: Annotated[list[str], operator.add]


# ------------------------------- 语料 -------------------------------

_DOCS: dict[str, list[str]] = {
    "vpn": ["认证失败通常有三类原因：密码过期、动态口令偏差、账号锁定。",
            "处理顺序：自助门户重置密码 → 校准手机时间。"],
    "email": ["单封邮件附件上限 25 MB，超出会被网关退回。"],
}

_FAIL_KEYWORD = "超时"


# ------------------------------- 节点 -------------------------------


def classify(state: RouteState) -> dict[str, Any]:
    text = f"{state.get('title', '')} {state.get('description', '')}".lower()
    if any(word in text for word in ("vpn", "认证", "隧道")):
        intent = "vpn"
    elif any(word in text for word in ("邮箱", "邮件", "附件")):
        intent = "email"
    else:
        intent = "other"
    return {"intent": intent, "trace": ["classify"]}


def _fetch(state: RouteState, intent: str, *, empty_rounds: int) -> dict[str, Any]:
    """取资料。`empty_rounds` 用来模拟"知识库前几次没查到"。"""
    attempts = state.get("attempts", 0) + 1

    # 模拟下游故障 → 写 error，由条件边导到兜底节点（这就是"错误边"的惯例做法）
    if _FAIL_KEYWORD in state.get("description", ""):
        return {
            "attempts": attempts,
            "error": f"{intent} 检索服务超时",
            "trace": [f"fetch_{intent}"],
        }

    docs = list(_DOCS.get(intent, [])) if attempts > empty_rounds else []
    return {
        "attempts": attempts,
        "docs": docs,
        "error": None,
        "trace": [f"fetch_{intent}"],
    }


def clarify(state: RouteState) -> dict[str, Any]:
    """信息不足时的追问分支。"""
    return {
        "answer": "还需要更多信息才能判断，请补充工单标题或具体报错。",
        "trace": ["clarify"],
    }


def answer(state: RouteState) -> dict[str, Any]:
    docs = state.get("docs") or []
    return {
        "answer": "根据资料：" + " ".join(docs) + "[C1]",
        "trace": ["answer"],
    }


def fallback(state: RouteState) -> dict[str, Any]:
    """兜底：任何异常都到这里，给用户一句可行动的话。"""
    return {
        "answer": f"查询暂时失败（{state.get('error') or '未知错误'}），请稍后重试或转人工。",
        "trace": ["fallback"],
    }


# ------------------------------- 路由 -------------------------------


def route_by_intent(state: RouteState) -> Literal["vpn", "email", "other"]:
    """三路分支：这是 add_conditional_edges 的 router。"""
    intent = state.get("intent", "other")
    return intent if intent in ("vpn", "email") else "other"  # type: ignore[return-value]


def route_after_fetch(state: RouteState) -> Literal["answer", "retry", "clarify", "fallback"]:
    """取完资料之后去哪：成功 → answer；可重试 → retry；超限 → clarify；出错 → fallback。"""
    if state.get("error"):
        return "fallback"
    if state.get("docs"):
        return "answer"
    if state.get("attempts", 0) < state.get("max_attempts", 3):
        return "retry"
    return "clarify"


# ------------------------------- 组装 -------------------------------


def build_branching_graph(*, empty_rounds: int = 1, max_attempts: int = 3):
    """条件边版本：三路分支 + 重试循环 + 错误兜底。"""

    def fetch_vpn(state: RouteState) -> dict[str, Any]:
        return _fetch(state, "vpn", empty_rounds=empty_rounds)

    def fetch_email(state: RouteState) -> dict[str, Any]:
        return _fetch(state, "email", empty_rounds=empty_rounds)

    builder = StateGraph(RouteState)
    builder.add_node("classify", classify)
    builder.add_node("fetch_vpn", fetch_vpn)
    builder.add_node("fetch_email", fetch_email)
    builder.add_node("answer", answer)
    builder.add_node("clarify", clarify)
    builder.add_node("fallback", fallback)

    builder.add_edge(START, "classify")
    builder.add_conditional_edges(
        "classify",
        route_by_intent,
        {"vpn": "fetch_vpn", "email": "fetch_email", "other": "clarify"},
    )
    for node in ("fetch_vpn", "fetch_email"):
        builder.add_conditional_edges(
            node,
            route_after_fetch,
            {
                "answer": "answer",
                "retry": node,          # ← 循环：回到自己
                "clarify": "clarify",
                "fallback": "fallback",
            },
        )
    builder.add_edge("answer", END)
    builder.add_edge("clarify", END)
    builder.add_edge("fallback", END)

    return builder.compile()


def build_command_graph(*, empty_rounds: int = 1):
    """Command 版本：节点自己决定去哪，不需要单独的 router 函数。"""

    def smart_fetch(state: RouteState) -> Command:
        intent = state.get("intent", "other")
        update = _fetch(state, intent, empty_rounds=empty_rounds)

        if update.get("error"):
            return Command(goto="fallback", update=update)
        if update.get("docs"):
            return Command(goto="answer", update=update)
        if update.get("attempts", 0) < state.get("max_attempts", 3):
            return Command(goto="smart_fetch", update=update)   # 自环
        return Command(goto="clarify", update=update)

    builder = StateGraph(RouteState)
    builder.add_node("classify", classify)
    builder.add_node("smart_fetch", smart_fetch)
    builder.add_node("answer", answer)
    builder.add_node("clarify", clarify)
    builder.add_node("fallback", fallback)

    builder.add_edge(START, "classify")
    builder.add_conditional_edges(
        "classify",
        route_by_intent,
        {"vpn": "smart_fetch", "email": "smart_fetch", "other": "clarify"},
    )
    builder.add_edge("answer", END)
    builder.add_edge("clarify", END)
    builder.add_edge("fallback", END)

    return builder.compile()


def describe_routes(graph: Any) -> list[tuple[str, str, str]]:
    """把（含条件边的）边列表摊平：source → target（条件标签）。"""
    rows: list[tuple[str, str, str]] = []
    for edge in graph.get_graph().edges:
        rows.append((edge.source, edge.target, getattr(edge, "data", "") or ""))
    return rows
