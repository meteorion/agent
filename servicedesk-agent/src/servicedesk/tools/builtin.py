# file: src/servicedesk/tools/builtin.py
"""三个演示用工具：查资产、查权限、查工单。

注意 description 和 Field(description=...) 的写法 —— **它们就是 prompt**。
模型完全靠这些文字决定"要不要调用、传什么参数"，写得含糊它就会选错。
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from pydantic import BaseModel, Field

from servicedesk.models.ticket import Priority
from servicedesk.rag.embedder import HashEmbedder
from servicedesk.rag.splitter import split_markdown
from servicedesk.rag.store import QdrantStore, index_chunks, rerank, retrieve
from servicedesk.tools.registry import InputRequest, NeedsInput, ToolRegistry


# ------------------------------- 参数模型 -------------------------------


class QueryAssetParams(BaseModel):
    hostname: str = Field(
        min_length=3,
        max_length=32,
        description="设备主机名，形如 NB-1024。只填主机名，不要填 IP 或使用者姓名。",
        examples=["NB-1024"],
    )


class QueryPermissionParams(BaseModel):
    employee_id: str = Field(
        pattern=r"^E-\d{4}$",
        description="员工编号，形如 E-2048。不是工号也不是邮箱前缀。",
        examples=["E-2048"],
    )


class QueryTicketParams(BaseModel):
    ticket_id: str = Field(
        pattern=r"^T-\d{4,}$",
        description="工单编号，形如 T-1001。",
        examples=["T-1001"],
    )


class SearchKnowledgeParams(BaseModel):
    query: str = Field(min_length=2, max_length=200, description="自然语言问题，如「VPN 认证失败怎么办」")
    top_k: int = Field(default=3, ge=1, le=5, description="返回几条，默认 3")


class CreateTicketParams(BaseModel):
    """建单参数。priority / approver 允许缺省——缺了会走 MRTR 向对方索取。"""

    title: str = Field(min_length=4, max_length=80, description="工单标题")
    priority: Priority | None = Field(default=None, description="优先级 P0–P3")
    approver: str | None = Field(default=None, description="审批人工号，形如 E-2048")


# -------------------------------- 假数据 --------------------------------

ASSETS: dict[str, dict[str, Any]] = {
    "NB-1024": {
        "hostname": "NB-1024",
        "owner": "张伟（E-2048）",
        "model": "ThinkPad X1 Carbon",
        "status": "在用",
        "warranty_until": "2027-03-31",
    },
    "NB-2077": {
        "hostname": "NB-2077",
        "owner": "李娜（E-3102）",
        "model": "MacBook Pro 14",
        "status": "在用",
        "warranty_until": "2026-12-15",
    },
}

PERMISSIONS: dict[str, dict[str, Any]] = {
    "E-2048": {
        "employee_id": "E-2048",
        "name": "张伟",
        "vpn": True,
        "systems": ["财务共享", "工单系统"],
        "note": "VPN 权限有效期至 2027-01-01",
    },
    "E-3102": {
        "employee_id": "E-3102",
        "name": "李娜",
        "vpn": False,
        "systems": ["工单系统"],
        "note": "未开通 VPN，如需要请提交账号权限工单",
    },
}

TICKETS: dict[str, dict[str, Any]] = {
    "T-1001": {
        "ticket_id": "T-1001",
        "status": "处理中",
        "priority": "P1",
        "assignee": "网络组-王强",
        "updated_at": "2026-11-17T09:30:00+08:00",
    }
}


# -------------------------------- 工具实现 --------------------------------


async def _query_asset(hostname: str) -> dict[str, Any]:
    await asyncio.sleep(0.05)                      # 模拟一次远程查询
    asset = ASSETS.get(hostname)
    if asset is None:
        # 注意：这是"查询无结果"，不是"工具出错"。
        # 返回一个结构化结果，模型才知道该怎么告诉用户。
        return {"hostname": hostname, "found": False, "message": "资产库中没有该设备"}
    return {"found": True, **asset}


async def _query_permission(employee_id: str) -> dict[str, Any]:
    await asyncio.sleep(0.05)
    record = PERMISSIONS.get(employee_id)
    if record is None:
        return {"employee_id": employee_id, "found": False, "message": "没有该员工的权限记录"}
    return {"found": True, **record}


async def _query_ticket(ticket_id: str) -> dict[str, Any]:
    await asyncio.sleep(0.05)
    ticket = TICKETS.get(ticket_id)
    if ticket is None:
        return {"ticket_id": ticket_id, "found": False, "message": "没有该工单"}
    return {"found": True, **ticket}


async def _create_ticket(
    title: str,
    priority: Priority | None = None,
    approver: str | None = None,
) -> dict[str, Any]:
    """建单。缺关键信息时**不报错**，而是抛 NeedsInput 让调用方补一轮。

    这就是 MRTR 的服务端一侧：**"我还需要什么"也是返回值的一部分。**
    """
    requests: list[InputRequest] = []
    if priority is None:
        requests.append(
            InputRequest(
                id="priority",
                prompt="这条工单的优先级是？",
                schema={"enum": ["P0", "P1", "P2", "P3"]},
            )
        )
    if approver is None:
        requests.append(
            InputRequest(
                id="approver",
                prompt="谁是这条工单的审批人？（P0/P1 必须有人审批）",
                schema={"type": "string", "examples": ["E-2048"]},
            )
        )
    if requests:
        raise NeedsInput(requests)

    # 幂等键：同样的标题 + 优先级 → 生成同一个工单号，重试不会重复建单
    digest = hashlib.sha1(f"{title}|{priority}".encode()).hexdigest()[:4].upper()
    return {
        "ticket_id": f"T-{digest}",
        "title": title,
        "priority": priority.value if isinstance(priority, Priority) else priority,
        "approver": approver,
        "status": "已创建",
    }


# ------------------------------ 知识库检索 ------------------------------


_KNOWLEDGE: dict[str, Any] = {}


async def _ensure_knowledge() -> tuple[QdrantStore, HashEmbedder]:
    """惰性建索引：只在第一次调用时加载语料，冷启动不被拖慢。"""
    if "store" in _KNOWLEDGE:
        return _KNOWLEDGE["store"], _KNOWLEDGE["embedder"]

    from pathlib import Path

    knowledge_dir = Path(__file__).resolve().parents[3] / "knowledge"
    chunks = []
    for path in sorted(knowledge_dir.glob("*.md")):
        chunks.extend(
            split_markdown(path.read_text(encoding="utf-8"), path.stem,
                           max_chars=400, overlap_chars=80)
        )
    embedder = HashEmbedder()
    store = QdrantStore(dim=embedder.dim, collection="builtin_knowledge")
    store.recreate()
    await index_chunks(store, chunks, embedder)
    _KNOWLEDGE["store"] = store
    _KNOWLEDGE["embedder"] = embedder
    return store, embedder


async def _search_knowledge(query: str, top_k: int = 3) -> dict[str, Any]:
    """把 RAG 检索做成 MCP 工具：Agent 不用自己拼向量库，调工具就行。"""
    store, embedder = await _ensure_knowledge()
    hits = await retrieve(store, embedder, query, top_k=max(top_k * 2, 6))
    ranked = rerank(query, hits, top_k=top_k)
    return {
        "query": query,
        "found": bool(ranked) and (ranked[0].rerank_score or 0) > 0,
        "hits": [
            {
                "chunk_id": hit.chunk_id,
                "heading": " > ".join(hit.heading_path),
                "score": round(hit.rerank_score or 0.0, 3),
                "text": hit.text[:280],
            }
            for hit in ranked
        ],
    }


# -------------------------------- 注册 --------------------------------


def build_default_registry() -> ToolRegistry:
    """装配默认工具集。生产里通常按"角色/场景"装配不同集合。"""
    registry = ToolRegistry()

    registry.register(
        name="query_asset",
        description=(
            "查询公司设备（笔记本/台式机）的资产信息，返回使用人、型号、状态与保修期。"
            "当用户询问某台设备归谁用、什么型号、还在不在保修期时使用。"
            "不要用它查员工权限或工单状态。"
        ),
        params_model=QueryAssetParams,
        timeout_s=2.0,
    )(_query_asset)

    registry.register(
        name="query_permission",
        description=(
            "查询员工的系统权限，返回是否开通 VPN、可访问哪些业务系统。"
            "当用户询问某人有没有某个权限、能不能访问某个系统时使用。"
            "不要用它查设备信息。"
        ),
        params_model=QueryPermissionParams,
        timeout_s=2.0,
    )(_query_permission)

    registry.register(
        name="query_ticket",
        description=(
            "查询 IT 工单的当前状态、优先级与处理人。"
            "当用户询问某个工单的进度时使用。"
        ),
        params_model=QueryTicketParams,
        timeout_s=2.0,
    )(_query_ticket)

    registry.register(
        name="search_knowledge",
        description=(
            "检索公司内部知识库（VPN、邮箱等手册），返回相关段落与出处。"
            "当用户询问「怎么办」「为什么」「怎么解决」这类需要查文档的问题时使用。"
        ),
        params_model=SearchKnowledgeParams,
        timeout_s=5.0,
    )(_search_knowledge)

    registry.register(
        name="create_ticket",
        description=(
            "创建一条 IT 工单。当用户明确要求报障、提交请求时使用。"
            "priority 与 approver 可以缺省，缺了会向调用方索取。"
        ),
        params_model=CreateTicketParams,
        timeout_s=3.0,
    )(_create_ticket)

    return registry
