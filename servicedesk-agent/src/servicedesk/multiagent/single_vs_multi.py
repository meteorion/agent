# file: src/servicedesk/multiagent/single_vs_multi.py
"""单图 vs 多 Agent：用同一批任务做对照，用数据说话。

本节刻意**不给结论站台**，只提供一把尺子：

    成功率    —— 最终答复对不对
    延迟      —— 墙钟耗时
    交接次数  —— Agent 之间传了几次话
    上下文量  —— 每次交接搬了多少字节（token 成本的代理指标）

同一个任务集，跑两套实现，把四个数字摆在一起。
大多数团队需要的答案是"单图就够"——但你要能**用数据**说出这句话。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

import operator
from langgraph.graph import END, START, StateGraph

# ------------------------------- 任务集 -------------------------------


@dataclass(frozen=True)
class Task:
    """一道题：输入 + 期望的答案特征（用于自动判分）。"""

    ticket_id: str
    title: str
    description: str
    expected_intent: str
    expected_keyword: str
    needs_approval: bool


TASKS: list[Task] = [
    Task("T-1", "VPN 连不上", "客户端提示认证失败", "vpn", "重置", True),
    Task("T-2", "邮件附件太大", "50MB 附件发不出去", "email", "25 MB", False),
    Task("T-3", "打印机坏了", "三楼打印机不出纸", "other", "补充信息", False),
    Task("T-4", "VPN 认证失败", "密码过期了", "vpn", "重置", True),
    Task("T-5", "邮箱收不到邮件", "附件被退回", "email", "25 MB", False),
]


# ------------------------------- 语料 -------------------------------

_DOCS: dict[str, list[str]] = {
    "vpn": ["认证失败通常有三类原因：密码过期、动态口令偏差、账号锁定。",
            "处理顺序：自助门户重置密码 → 校准手机时间。"],
    "email": ["单封邮件附件上限 25 MB，超出会被网关退回。"],
}


def _classify(title: str, description: str) -> str:
    text = f"{title} {description}".lower()
    if any(word in text for word in ("vpn", "认证", "隧道")):
        return "vpn"
    if any(word in text for word in ("邮箱", "邮件", "附件")):
        return "email"
    return "other"


# =========================== 实现 A：单图 ===========================


class SingleState(TypedDict, total=False):
    ticket_id: str
    title: str
    description: str
    intent: str
    docs: list[str]
    draft: str
    final: str
    approval_needed: bool
    trace: Annotated[list[str], operator.add]


def _s_classify(state: SingleState) -> dict[str, Any]:
    return {"intent": _classify(state.get("title", ""), state.get("description", "")),
            "trace": ["classify"]}


def _s_retrieve(state: SingleState) -> dict[str, Any]:
    return {"docs": list(_DOCS.get(state.get("intent", "other"), [])), "trace": ["retrieve"]}


def _s_draft(state: SingleState) -> dict[str, Any]:
    docs = state.get("docs") or []
    if docs:
        draft = "根据资料：" + " ".join(docs) + "[C1]"
    else:
        draft = "资料中没有相关信息，请补充信息。"
    return {"draft": draft, "trace": ["draft"]}


def _s_review(state: SingleState) -> dict[str, Any]:
    draft = state.get("draft") or ""
    # 有资料就必须带引用；这是唯一的质量门
    ok = ("[C1]" in draft) or ("补充信息" in draft)
    return {"final": draft if ok else "审查未通过：缺少引用", "trace": ["review"]}


def build_single_agent_graph():
    builder = StateGraph(SingleState)
    builder.add_node("classify", _s_classify)
    builder.add_node("retrieve", _s_retrieve)
    builder.add_node("draft", _s_draft)
    builder.add_node("review", _s_review)
    builder.add_edge(START, "classify")
    builder.add_edge("classify", "retrieve")
    builder.add_edge("retrieve", "draft")
    builder.add_edge("draft", "review")
    builder.add_edge("review", END)
    return builder.compile()


# ========================= 实现 B：多 Agent =========================


class MultiState(TypedDict, total=False):
    ticket_id: str
    title: str
    description: str
    # Agent 之间的交接记录：每跳都把完整上下文搬一遍
    messages: Annotated[list[dict[str, Any]], operator.add]
    findings: str
    draft: str
    final: str
    next_agent: str
    rounds: int
    trace: Annotated[list[str], operator.add]


HANDOFF_COST = {"bytes": 0}          # 观测用：累计交接的字节数


def _handoff(state: MultiState, sender: str, receiver: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Agent 之间的一次交接：把上下文序列化后塞进消息列表。

    真实多 Agent 框架里，这一步就是"把历史重新喂进另一个 prompt"——
    **成本主要来自这里**，而不是来自"多了一个 Agent"。
    """
    message = {"from": sender, "to": receiver, **payload}
    HANDOFF_COST["bytes"] += len(json.dumps(message, ensure_ascii=False).encode())
    return {"messages": [message], "next_agent": receiver, "trace": [f"handoff:{sender}->{receiver}"]}


def _supervisor(state: MultiState) -> dict[str, Any]:
    """调度：谁缺什么就派谁去。"""
    if not state.get("findings"):
        return {"next_agent": "researcher", "trace": ["supervisor:researcher"]}
    if not state.get("draft"):
        return {"next_agent": "writer", "trace": ["supervisor:writer"]}
    if not state.get("final"):
        return {"next_agent": "reviewer", "trace": ["supervisor:reviewer"]}
    return {"next_agent": "done", "trace": ["supervisor:done"]}


def _researcher(state: MultiState) -> dict[str, Any]:
    intent = _classify(state.get("title", ""), state.get("description", ""))
    docs = _DOCS.get(intent, [])
    findings = json.dumps({"intent": intent, "docs": docs}, ensure_ascii=False)
    return _handoff(state, "researcher", "supervisor", {"findings": findings}) | {"findings": findings}


def _writer(state: MultiState) -> dict[str, Any]:
    findings = json.loads(state.get("findings") or "{}")
    docs = findings.get("docs") or []
    draft = ("根据资料：" + " ".join(docs) + "[C1]") if docs else "资料中没有相关信息，请补充信息。"
    return _handoff(state, "writer", "supervisor", {"draft": draft}) | {"draft": draft}


def _reviewer(state: MultiState) -> dict[str, Any]:
    draft = state.get("draft") or ""
    rounds = state.get("rounds", 0)
    ok = ("[C1]" in draft) or ("补充信息" in draft)
    final = draft if ok else "审查未通过：缺少引用"

    # 不通过就打回 writer 一次（模拟多 Agent 常见的返工）
    if not ok and rounds < 1:
        return _handoff(state, "reviewer", "writer", {"final": "", "rounds": rounds + 1}) | {
            "draft": "", "rounds": rounds + 1,
        }
    return _handoff(state, "reviewer", "end", {"final": final}) | {"final": final}


def _route_after_supervisor(state: MultiState) -> str:
    return state.get("next_agent", "done")


def build_multi_agent_graph():
    builder = StateGraph(MultiState)
    builder.add_node("supervisor", _supervisor)
    builder.add_node("researcher", _researcher)
    builder.add_node("writer", _writer)
    builder.add_node("reviewer", _reviewer)

    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        _route_after_supervisor,
        {"researcher": "researcher", "writer": "writer", "reviewer": "reviewer", "done": END},
    )
    for worker in ("researcher", "writer", "reviewer"):
        builder.add_edge(worker, "supervisor")
    return builder.compile()


# ------------------------------- 跑分 -------------------------------


@dataclass
class Result:
    engine: str
    success: int = 0
    total: int = 0
    latency_ms: float = 0.0
    handoffs: int = 0
    context_bytes: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "success": self.success,
            "total": self.total,
            "success_rate": round(self.success_rate, 3),
            "latency_ms": round(self.latency_ms, 1),
            "handoffs": self.handoffs,
            "context_bytes": self.context_bytes,
            "failures": self.failures,
        }


async def run_single(tasks: list[Task]) -> Result:
    graph = build_single_agent_graph()
    result = Result(engine="single")

    for task in tasks:
        started = time.perf_counter()
        out = await graph.ainvoke(
            {"ticket_id": task.ticket_id, "title": task.title,
             "description": task.description, "trace": []}
        )
        result.latency_ms += (time.perf_counter() - started) * 1000
        result.total += 1

        final = out.get("final") or ""
        ok = task.expected_keyword in final
        if ok:
            result.success += 1
        else:
            result.failures.append(f"{task.ticket_id}: 期望含 {task.expected_keyword!r}，实际 {final[:40]!r}")

    return result


async def run_multi(tasks: list[Task]) -> Result:
    graph = build_multi_agent_graph()
    result = Result(engine="multi")

    for task in tasks:
        HANDOFF_COST["bytes"] = 0
        started = time.perf_counter()
        out = await graph.ainvoke(
            {"ticket_id": task.ticket_id, "title": task.title,
             "description": task.description, "trace": [], "messages": []},
            {"recursion_limit": 40},
        )
        result.latency_ms += (time.perf_counter() - started) * 1000
        result.total += 1
        result.handoffs += len([t for t in out.get("trace", []) if t.startswith("handoff:")])
        result.context_bytes += HANDOFF_COST["bytes"]

        final = out.get("final") or ""
        ok = task.expected_keyword in final
        if ok:
            result.success += 1
        else:
            result.failures.append(f"{task.ticket_id}: 期望含 {task.expected_keyword!r}，实际 {final[:40]!r}")

    return result


async def compare(tasks: list[Task] | None = None) -> dict[str, Any]:
    tasks = tasks or TASKS
    single = await run_single(tasks)
    multi = await run_multi(tasks)
    return {"single": single.as_dict(), "multi": multi.as_dict()}


# --------------------- 第二个场景：工具上下文压力 ---------------------


def compare_tool_context() -> dict[str, Any]:
    """单 Agent 扛全部工具 vs 每个专家只扛一组。

    这是多 Agent **真正**的优势场景：不是"分工好看"，
    而是**每次调用的 prompt 里少塞几千字节的 schema**。
    """
    from servicedesk.tools.builtin import build_default_registry

    registry = build_default_registry()
    schemas = {
        item["function"]["name"]: item
        for item in registry.schemas()
    }

    def size(items: list[str]) -> int:
        return len(json.dumps([schemas[name] for name in items], ensure_ascii=False).encode())

    groups: dict[str, list[str]] = {
        "asset": ["query_asset", "query_permission"],
        "ticket": ["query_ticket", "create_ticket"],
        "knowledge": ["search_knowledge"],
    }
    all_tools = list(schemas)

    # 单 Agent：一次任务要 3 次调用，每次都得带上全部 schema
    single_per_call = size(all_tools)
    single_total = single_per_call * 3

    # 多 Agent：每次只带自己那组 + 一份路由清单
    routing_manifest = len(json.dumps(
        [{"name": k, "tools": v} for k, v in groups.items()], ensure_ascii=False
    ).encode())
    multi_per_call = {name: size(tools) for name, tools in groups.items()}
    multi_total = sum(multi_per_call.values()) + routing_manifest * 3

    return {
        "工具总数": len(all_tools),
        "单 Agent 每次调用 schema 字节": single_per_call,
        "单 Agent 三次调用合计": single_total,
        "多 Agent 各组每次": multi_per_call,
        "多 Agent 合计（含路由清单 x3）": multi_total,
        "节省比例": round(1 - multi_total / single_total, 3) if single_total else 0.0,
    }
