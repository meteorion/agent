# file: src/servicedesk/multiagent/topologies.py
"""两种多 Agent 拓扑：中心调度（supervisor） vs 对等转移（handoff）。

形状差别只有一句话：

    supervisor：worker 干完**回中心**，由中心决定下一个是谁
    handoff   ：worker 干完**直接交给下一个**，没有中心

前者看得见全局，代价是每次都要绕回中心；
后者少一跳，代价是**没有人知道全局**——包括"我是不是被踢回来了"。
"""

from __future__ import annotations

import json
import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

# ------------------------------- State -------------------------------


class TopoState(TypedDict, total=False):
    ticket_id: str
    title: str
    description: str

    stage: str          # triaged / researched / written / reviewed / finalized
    intent: str
    docs: list[str]
    draft: str
    review: str
    final: str

    turns: int                                   # 节点执行次数
    visits: dict[str, int]                       # 每个 agent 被访问次数（防踢皮球）
    trace: Annotated[list[str], operator.add]
    notes: Annotated[list[str], operator.add]


_DOCS: dict[str, list[str]] = {
    "vpn": ["认证失败常见原因：密码过期、动态口令偏差、账号锁定。",
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


# ====================== 拓扑 A：supervisor（中心调度）======================


def _sup_triage(state: TopoState) -> dict[str, Any]:
    intent = _classify(state.get("title", ""), state.get("description", ""))
    return {"intent": intent, "stage": "triaged", "turns": state.get("turns", 0) + 1,
            "trace": ["triage"]}


def _sup_research(state: TopoState) -> dict[str, Any]:
    docs = list(_DOCS.get(state.get("intent", "other"), []))
    return {"docs": docs, "stage": "researched", "turns": state.get("turns", 0) + 1,
            "trace": ["researcher"]}


def _sup_write(state: TopoState) -> dict[str, Any]:
    docs = state.get("docs") or []
    draft = ("根据资料：" + " ".join(docs) + "[C1]") if docs else ""
    return {"draft": draft, "stage": "written", "turns": state.get("turns", 0) + 1,
            "trace": ["writer"]}


def _sup_review(state: TopoState) -> dict[str, Any]:
    draft = state.get("draft") or ""
    if not draft:
        review = "no_draft"
    elif "[C1]" not in draft:
        review = "no_citation"
    else:
        review = "ok"
    return {"review": review, "stage": "reviewed", "turns": state.get("turns", 0) + 1,
            "trace": ["reviewer"]}


def _sup_finalize(state: TopoState) -> dict[str, Any]:
    if state.get("review") == "ok":
        final = state.get("draft") or ""
    else:
        final = "资料不足或未通过审查，已转人工。"
    return {"final": final, "stage": "finalized", "turns": state.get("turns", 0) + 1,
            "trace": ["finalize"]}


_STAGE_NEXT = {
    "": "triage",
    "triaged": "research",
    "researched": "write",
    "written": "review",
    "reviewed": "finalize",
}


def _route_supervisor(state: TopoState) -> str:
    """按**显式阶段**路由。

    教训（本节踩过）：原先用"字段是否为空"推断进度，结果"无资料"的任务
    `docs` 一直为空，supervisor 就一直派 research —— 死循环。
    进度必须是**显式状态**，不能靠数据形状猜。
    """
    return _STAGE_NEXT.get(state.get("stage", ""), "finalize")


def _supervisor(state: TopoState) -> dict[str, Any]:
    target = _route_supervisor(state)
    return {
        "turns": state.get("turns", 0) + 1,
        "trace": [f"supervisor->{target}"],
        "notes": [f"supervisor 派给 {target}"],
    }


def build_supervisor_graph():
    builder = StateGraph(TopoState)
    builder.add_node("supervisor", _supervisor)
    builder.add_node("triage", _sup_triage)
    builder.add_node("research", _sup_research)
    builder.add_node("write", _sup_write)
    builder.add_node("review", _sup_review)
    builder.add_node("finalize", _sup_finalize)

    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        _route_supervisor,
        {"triage": "triage", "research": "research", "write": "write",
         "review": "review", "finalize": "finalize"},
    )
    for worker in ("triage", "research", "write", "review"):
        builder.add_edge(worker, "supervisor")     # ← 干完回中心
    builder.add_edge("finalize", END)
    return builder.compile()


# ====================== 拓扑 B：handoff（对等转移）======================


def _visits(state: TopoState) -> dict[str, int]:
    return dict(state.get("visits") or {})


def _bump(state: TopoState, name: str) -> dict[str, int]:
    visits = _visits(state)
    visits[name] = visits.get(name, 0) + 1
    return visits


def _ho_triage(state: TopoState) -> Command:
    intent = _classify(state.get("title", ""), state.get("description", ""))
    visits = _bump(state, "triage")
    # 没有中心，自己决定交给谁
    return Command(
        goto="research",
        update={
            "intent": intent, "visits": visits,
            "turns": state.get("turns", 0) + 1,
            "trace": ["triage"],
            "notes": ["triage 直接交给 research"],
        },
    )


def _ho_research(state: TopoState) -> Command:
    docs = list(_DOCS.get(state.get("intent", "other"), []))
    visits = _bump(state, "research")
    return Command(
        goto="write",
        update={
            "docs": docs, "visits": visits,
            "turns": state.get("turns", 0) + 1,
            "trace": ["researcher"],
            "notes": ["research 直接交给 write"],
        },
    )


def _ho_write(state: TopoState) -> Command:
    docs = state.get("docs") or []
    draft = ("根据资料：" + " ".join(docs) + "[C1]") if docs else ""
    visits = _bump(state, "write")

    # 防"踢皮球"：同一个 agent 被访问超过 2 次就止损（handoff 拓扑必须自己管这件事）
    if visits.get("write", 0) > 2:
        return Command(
            goto=END,
            update={
                "draft": draft, "visits": visits,
                "turns": state.get("turns", 0) + 1,
                "final": "来回改写超过 2 次，已止损转人工。",
                "trace": ["writer:abort"],
                "notes": ["writer 检测到自己被反复调用，止损"],
            },
        )

    return Command(
        goto="review",
        update={
            "draft": draft, "visits": visits,
            "turns": state.get("turns", 0) + 1,
            "trace": ["writer"],
            "notes": ["write 直接交给 review"],
        },
    )


def _ho_review(state: TopoState) -> Command:
    draft = state.get("draft") or ""
    visits = _bump(state, "review")

    if draft and "[C1]" in draft:
        return Command(
            goto=END,
            update={
                "review": "ok", "final": draft, "visits": visits,
                "turns": state.get("turns", 0) + 1,
                "trace": ["reviewer:ok"],
                "notes": ["review 通过，结束"],
            },
        )

    # 不合格 → 直接打回 writer（这就是 handoff 的"对等回退"）
    return Command(
        goto="write",
        update={
            "review": "no_citation", "visits": visits,
            "turns": state.get("turns", 0) + 1,
            "trace": ["reviewer:reject"],
            "notes": ["review 打回 write"],
        },
    )


def build_handoff_graph():
    builder = StateGraph(TopoState)
    builder.add_node("triage", _ho_triage)
    builder.add_node("research", _ho_research)
    builder.add_node("write", _ho_write)
    builder.add_node("review", _ho_review)

    builder.add_edge(START, "triage")
    builder.add_edge("review", END)      # 兜底；实际跳转由 Command 决定
    return builder.compile()


# ------------------------------- 对比跑分 -------------------------------


@dataclass
class TopoResult:
    topology: str
    tasks: int = 0
    success: int = 0
    turns: int = 0
    notes: int = 0
    trace_len: int = 0
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "topology": self.topology,
            "tasks": self.tasks,
            "success": self.success,
            "turns_per_task": round(self.turns / self.tasks, 2) if self.tasks else 0.0,
            "notes_per_task": round(self.notes / self.tasks, 2) if self.tasks else 0.0,
            "trace_len_per_task": round(self.trace_len / self.tasks, 2) if self.tasks else 0.0,
            "failures": self.failures,
        }


TASKS: list[tuple[str, str, str, str]] = [
    ("T-1", "VPN 连不上", "客户端提示认证失败", "重置"),
    ("T-2", "邮件附件太大", "50MB 发不出去", "25 MB"),
    ("T-3", "打印机坏了", "三楼不出纸", "转人工"),
]


async def _run(graph: Any, topology: str) -> TopoResult:
    result = TopoResult(topology=topology)
    for ticket_id, title, description, keyword in TASKS:
        out = await graph.ainvoke(
            {"ticket_id": ticket_id, "title": title, "description": description,
             "trace": [], "notes": []},
            {"recursion_limit": 40},
        )
        result.tasks += 1
        final = out.get("final") or ""
        if keyword in final:
            result.success += 1
        else:
            result.failures.append(f"{ticket_id}: 期望含 {keyword!r}，实际 {final[:40]!r}")
        result.turns += out.get("turns", 0)
        result.notes += len(out.get("notes") or [])
        result.trace_len += len(out.get("trace") or [])
    return result


async def compare_topologies() -> dict[str, Any]:
    supervisor_result = await _run(build_supervisor_graph(), "supervisor")
    handoff_result = await _run(build_handoff_graph(), "handoff")
    return {
        "supervisor": supervisor_result.as_dict(),
        "handoff": handoff_result.as_dict(),
    }
