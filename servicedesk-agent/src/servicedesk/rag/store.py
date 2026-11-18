# file: src/servicedesk/rag/store.py
"""向量库：建集合、写块、检索、过滤、阈值、rerank。

用 Qdrant 的本地内存模式（QdrantClient(location=":memory:")），
API 与线上服务**完全一致**——换成本地 docker 或云上集群时，
只有构造参数变，下面每一行调用都不用改。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from typing import Sequence

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

from servicedesk.logging_setup import get_logger
from servicedesk.rag.embedder import EMBED_DIM, Embedder
from servicedesk.rag.splitter import Chunk

log = get_logger("rag.store")

# Qdrant 的 point id 只接受整数或 UUID，而我们的 chunk_id 是 "vpn-guide#003"。
# 用 uuid5 做稳定映射：同一个 chunk_id 永远得到同一个 id，重建索引也不会漂移。
_POINT_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, chunk_id))


# -------------------------------- 检索结果 --------------------------------


@dataclass(frozen=True)
class Hit:
    """一条检索命中。"""

    chunk_id: str
    doc_id: str
    order: int
    heading_path: list[str]
    text: str
    score: float                 # 向量相似度（余弦，未归一化前的原始分）
    rerank_score: float | None = None

    @property
    def citation(self) -> str:
        path = " > ".join(self.heading_path) or "（无标题）"
        return f"{self.doc_id} § {path}"

    def with_rerank(self, score: float) -> Hit:
        return replace(self, rerank_score=score)


# -------------------------------- 向量库 --------------------------------


class QdrantStore:
    """一层薄封装：只暴露项目真正用到的四个动作。"""

    def __init__(
        self,
        *,
        dim: int = EMBED_DIM,
        collection: str = "knowledge_chunks",
        client: QdrantClient | None = None,
    ) -> None:
        self.dim = dim
        self.collection = collection
        self.client = client or QdrantClient(location=":memory:")

    # -- 生命周期 --

    def recreate(self) -> None:
        """删掉重建。只在"换 embedding 模型"或首次建库时用。"""
        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
        )
        log.info("collection_created", collection=self.collection, dim=self.dim)

    def ensure(self) -> None:
        if not self.client.collection_exists(self.collection):
            self.recreate()

    def count(self) -> int:
        return self.client.count(self.collection).count

    # -- 写入 --

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> int:
        if len(chunks) != len(vectors):
            raise ValueError(f"块数({len(chunks)})与向量数({len(vectors)})不一致")
        if not chunks:
            return 0

        points = [
            PointStruct(
                id=point_id(chunk.chunk_id),
                vector=list(vector),
                payload={
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "order": chunk.order,
                    "heading_path": list(chunk.heading_path),
                    "text": chunk.text,
                },
            )
            for chunk, vector in zip(chunks, vectors)
        ]
        self.client.upsert(collection_name=self.collection, points=points)
        log.info("chunks_upserted", count=len(points), collection=self.collection)
        return len(points)

    # -- 检索 --

    def search(
        self,
        vector: Sequence[float],
        *,
        top_k: int = 5,
        doc_ids: Sequence[str] | None = None,
        min_score: float | None = None,
    ) -> list[Hit]:
        """top-k 检索。返回的列表可能为空——"检索为空"是正常结果，不是错误。"""
        query_filter = None
        if doc_ids:
            query_filter = Filter(
                must=[FieldCondition(key="doc_id", match=MatchAny(any=list(doc_ids)))]
            )

        # 多取一些候选，方便阈值过滤后仍有足够的条数
        response = self.client.query_points(
            collection_name=self.collection,
            query=list(vector),
            limit=max(top_k, 1) * 2,
            query_filter=query_filter,
            with_payload=True,
        )

        hits: list[Hit] = []
        for point in response.points:
            payload = point.payload or {}
            score = float(point.score)
            if min_score is not None and score < min_score:
                continue
            hits.append(
                Hit(
                    chunk_id=str(payload.get("chunk_id", "")),
                    doc_id=str(payload.get("doc_id", "")),
                    order=int(payload.get("order", 0)),
                    heading_path=list(payload.get("heading_path") or []),
                    text=str(payload.get("text", "")),
                    score=score,
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    def payload_filter_by_doc(self, doc_id: str) -> Filter:
        """示例：按文档过滤。生产里更常见的过滤条件是租户、权限、时间。"""
        return Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))])


# -------------------------------- rerank --------------------------------

_TERM_SPLIT = re.compile(r"[^\w\u4e00-\u9fff]+")


def query_terms(query: str) -> set[str]:
    """把查询拆成可匹配的词项：整词 + 字符二元组。

    中文没有空格，按二元组切是最省事且够用的做法
    （"认证失败" → 认证/证失/失败）。
    """
    terms: set[str] = set()
    for token in _TERM_SPLIT.split(query):
        if not token:
            continue
        if len(token) >= 2:
            terms.add(token)
        for index in range(len(token) - 1):
            terms.add(token[index : index + 2])
    return {term for term in terms if len(term) >= 2}


def rerank(query: str, hits: list[Hit], *, top_k: int = 3) -> list[Hit]:
    """轻量 rerank：词项覆盖率 + 标题命中加权。

    真正的 rerank 用 cross-encoder 模型（把 query 与 doc 拼起来过一次模型），
    效果更好但要多一个模型服务。这里先用一个**完全可解释**的替代，
    把链路跑通；将来换模型只需替换这个函数。
    """
    terms = query_terms(query)
    if not terms:
        return hits[:top_k]

    rescored: list[Hit] = []
    for hit in hits:
        text = hit.text.lower()
        heading = " > ".join(hit.heading_path).lower()
        text_coverage = sum(1 for term in terms if term in text) / len(terms)
        heading_coverage = sum(1 for term in terms if term in heading) / len(terms)
        score = 0.6 * text_coverage + 0.4 * heading_coverage
        rescored.append(hit.with_rerank(round(score, 4)))

    # 主键 rerank 分，次键保留向量分（同分时不让结果随机）
    rescored.sort(key=lambda hit: (hit.rerank_score or 0.0, hit.score), reverse=True)
    return rescored[:top_k]


# -------------------------------- 便捷函数 --------------------------------


async def index_chunks(
    store: QdrantStore,
    chunks: Sequence[Chunk],
    embedder: Embedder,
    *,
    batch_size: int = 16,
) -> int:
    """把块写进向量库。注意：算向量用的是 embedding_text（带标题路径）。"""
    store.ensure()
    written = 0
    for start in range(0, len(chunks), batch_size):
        batch = list(chunks[start : start + batch_size])
        vectors = await embedder.embed([chunk.embedding_text for chunk in batch])
        written += store.upsert(batch, vectors)
    return written


async def retrieve(
    store: QdrantStore,
    embedder: Embedder,
    query: str,
    *,
    top_k: int = 5,
    min_score: float | None = None,
    doc_ids: Sequence[str] | None = None,
) -> list[Hit]:
    """一步完成"查询向量化 → 检索"。"""
    (vector,) = await embedder.embed([query])
    return store.search(vector, top_k=top_k, min_score=min_score, doc_ids=doc_ids)
