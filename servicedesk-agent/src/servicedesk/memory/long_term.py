# file: src/servicedesk/memory/long_term.py
"""长期记忆：语义（用户偏好）+ 情景（历史事件）+ 遗忘。

和 4.4 的分工：

    短期（4.4）—— 这一轮塞不下，裁掉旧的
    长期（4.5）—— 下次对话还要记得，跨会话存

两类记忆**不是并列关系**，它们的检索逻辑不一样：

    语义记忆（semantic）：用户是谁、偏好什么 —— **几乎总要带上**，靠重要性排序
    情景记忆（episodic）：哪天发生了什么 —— **按当前问题相似度**召回

遗忘也是必须的：不遗忘的记忆，等于没有记忆（噪声越攒越多，召回越来越差）。
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from servicedesk.rag.embedder import HashEmbedder

DAY = 86_400.0

# 召回打分权重（三者之和为 1）
W_SIMILARITY = 0.60
W_IMPORTANCE = 0.25
W_RECENCY = 0.15

# 情景记忆的"新鲜度"半衰尺度（天）
RECENCY_SCALE_DAYS = 90.0

KIND_SEMANTIC = "semantic"
KIND_EPISODIC = "episodic"


# ------------------------------- 记忆条目 -------------------------------


@dataclass
class MemoryItem:
    id: str
    kind: str
    text: str
    user_id: str
    created_at: float
    importance: float = 0.5
    ttl_s: float | None = None          # None = 永不过期（但仍会衰减）
    tags: tuple[str, ...] = ()
    last_access_at: float = 0.0

    # -- 生命周期 --

    def expired(self, now: float) -> bool:
        return self.ttl_s is not None and (now - self.created_at) > self.ttl_s

    def age_days(self, now: float) -> float:
        return max(0.0, (now - self.created_at) / DAY)

    def decayed_importance(self, now: float) -> float:
        """重要性随时间衰减：越旧越不重要，但不会归零。"""
        age = self.age_days(now)
        return self.importance * math.exp(-age / (RECENCY_SCALE_DAYS * 3))

    def recency(self, now: float) -> float:
        return math.exp(-self.age_days(now) / RECENCY_SCALE_DAYS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "importance": self.importance,
            "tags": list(self.tags),
            "ttl_s": self.ttl_s,
        }


@dataclass
class Recalled:
    item: MemoryItem
    score: float
    similarity: float
    importance: float
    recency: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.item.id,
            "kind": self.item.kind,
            "text": self.item.text[:80],
            "score": round(self.score, 4),
            "similarity": round(self.similarity, 3),
            "importance": round(self.importance, 3),
            "recency": round(self.recency, 3),
        }


# ------------------------------- 余弦相似度 -------------------------------


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


# ------------------------------- 记忆库 -------------------------------


@dataclass
class MemoryStore:
    """内存实现。生产换向量库 + 关系库（语义/情景可以同库不同 collection）。"""

    embedder: HashEmbedder = field(default_factory=HashEmbedder)
    _items: dict[str, MemoryItem] = field(default_factory=dict)
    _vectors: dict[str, list[float]] = field(default_factory=dict)
    _forgotten: set[str] = field(default_factory=set)

    # -- 写入 --

    def remember(
        self,
        kind: str,
        text: str,
        *,
        user_id: str,
        now: float | None = None,
        importance: float = 0.5,
        ttl_s: float | None = None,
        tags: Iterable[str] = (),
    ) -> MemoryItem:
        if kind not in (KIND_SEMANTIC, KIND_EPISODIC):
            raise ValueError(f"未知记忆类型：{kind}")
        moment = time.time() if now is None else now
        item = MemoryItem(
            id=f"mem_{uuid.uuid4().hex[:10]}",
            kind=kind,
            text=text,
            user_id=user_id,
            created_at=moment,
            importance=max(0.0, min(1.0, importance)),
            ttl_s=ttl_s,
            tags=tuple(tags),
        )
        self._items[item.id] = item
        self._vectors[item.id] = self._embed_sync(text)
        return item

    def _embed_sync(self, text: str) -> list[float]:
        """HashEmbedder.embed 是异步的，这里用事件循环跑一次（demo/测试够用）。"""
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.embedder.embed([text]))[0]
        # 已在事件循环里：用同步版本
        from servicedesk.rag.embedder import hash_vector

        return hash_vector(text, self.embedder.dim)

    # -- 检索 --

    def recall(
        self,
        query: str,
        *,
        user_id: str,
        now: float | None = None,
        top_k: int = 3,
        kinds: Iterable[str] | None = None,
        include_expired: bool = False,
    ) -> list[Recalled]:
        """混合召回：情景按相似度，语义按重要性（几乎总带上）。

        **遗忘在这里生效**：过期与已删除的记忆不会被召回。
        """
        moment = time.time() if now is None else now
        query_vector = self._embed_sync(query)
        wanted = set(kinds) if kinds else {KIND_SEMANTIC, KIND_EPISODIC}

        scored: list[Recalled] = []
        for item in self._items.values():
            if item.id in self._forgotten or item.user_id != user_id:
                continue
            if item.kind not in wanted:
                continue
            if not include_expired and item.expired(moment):
                continue

            similarity = _cosine(query_vector, self._vectors.get(item.id, []))
            importance = item.decayed_importance(moment)
            recency = item.recency(moment)
            score = (
                W_SIMILARITY * similarity
                + W_IMPORTANCE * importance
                + W_RECENCY * recency
            )
            scored.append(Recalled(item, score, similarity, importance, recency))

        scored.sort(key=lambda row: row.score, reverse=True)
        top = scored[:top_k]
        for row in top:
            row.item.last_access_at = moment
        return top

    # -- 遗忘 --

    def forget(self, memory_id: str) -> bool:
        """显式删除（被遗忘权 / 用户要求删掉）。"""
        if memory_id not in self._items:
            return False
        self._forgotten.add(memory_id)
        self._items.pop(memory_id, None)
        self._vectors.pop(memory_id, None)
        return True

    def forget_user(self, user_id: str) -> int:
        """把一个用户的所有记忆清掉（GDPR 式删除）。"""
        ids = [item.id for item in self._items.values() if item.user_id == user_id]
        for memory_id in ids:
            self.forget(memory_id)
        return len(ids)

    def forget_expired(self, now: float | None = None) -> int:
        """清理过期记忆（后台任务定期跑）。"""
        moment = time.time() if now is None else now
        expired = [item.id for item in self._items.values() if item.expired(moment)]
        for memory_id in expired:
            self.forget(memory_id)
        return len(expired)

    # -- 观测 --

    def stats(self, now: float | None = None) -> dict[str, Any]:
        moment = time.time() if now is None else now
        items = list(self._items.values())
        return {
            "total": len(items),
            "semantic": sum(1 for item in items if item.kind == KIND_SEMANTIC),
            "episodic": sum(1 for item in items if item.kind == KIND_EPISODIC),
            "expired": sum(1 for item in items if item.expired(moment)),
            "forgotten": len(self._forgotten),
        }

    # -- 给 prompt 用的格式化 --

    def context_block(self, recalled: list[Recalled], *, now: float | None = None) -> str:
        """把召回结果拼成一段可塞进 prompt 的文本。

        **注意脱敏**：真实系统里这里要过滤手机号/身份证（1.4 的红线）。
        """
        moment = time.time() if now is None else now
        lines: list[str] = []
        for row in recalled:
            age = row.item.age_days(moment)
            when = "今天" if age < 1 else f"{age:.0f} 天前"
            label = "偏好" if row.item.kind == KIND_SEMANTIC else "历史"
            lines.append(f"[{label}·{when}] {row.item.text}")
        return "\n".join(lines)
