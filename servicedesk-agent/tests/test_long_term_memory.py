# file: tests/test_long_term_memory.py
"""4.5 的断言：三个月前的工单能召回、过期不被召回、遗忘生效。"""

from __future__ import annotations

import time

import pytest

from servicedesk.memory.long_term import (
    DAY,
    KIND_EPISODIC,
    KIND_SEMANTIC,
    MemoryStore,
)


@pytest.fixture()
def now() -> float:
    return 1_800_000_000.0        # 固定时钟，测试不依赖真实时间


def build(now: float) -> MemoryStore:
    store = MemoryStore()
    store.remember(KIND_SEMANTIC, "张伟偏好邮件联系，不喜欢电话打扰",
                   user_id="u-1", now=now - 3 * DAY, importance=0.9)
    store.remember(KIND_EPISODIC, "工单 T-1001：VPN 认证失败，最终通过重置密码解决",
                   user_id="u-1", now=now - 90 * DAY, importance=0.8, tags=("vpn",))
    store.remember(KIND_EPISODIC, "工单 T-0588：打印机卡纸，换纸盒解决",
                   user_id="u-1", now=now - 30 * DAY, importance=0.4)
    store.remember(KIND_EPISODIC, "临时验证码 123456，有效期十分钟",
                   user_id="u-1", now=now - 3600, importance=0.2, ttl_s=600)
    return store


# ------------------------------ 召回 ------------------------------


def test_three_month_old_ticket_is_recalled_first(now: float) -> None:
    """本节验收点：三个月前的同题工单，在新会话里排第一。"""
    store = build(now)
    hits = store.recall("VPN 认证失败怎么办", user_id="u-1", now=now, top_k=5)

    assert hits[0].item.text.startswith("工单 T-1001")
    assert hits[0].similarity > 0.3, "内容相关性应该是最高的一档"


def test_expired_memory_is_not_recalled(now: float) -> None:
    store = build(now)
    hits = store.recall("临时验证码", user_id="u-1", now=now, top_k=5)

    texts = " ".join(row.item.text for row in hits)
    assert "123456" not in texts, "TTL 过期的记忆不该被召回"


def test_include_expired_can_still_see_it(now: float) -> None:
    """调试/审计时需要看到过期条目，所以要能显式放开。"""
    store = build(now)
    hits = store.recall("临时验证码", user_id="u-1", now=now, top_k=5, include_expired=True)
    assert any("123456" in row.item.text for row in hits)


def test_semantic_memory_is_always_near_top(now: float) -> None:
    """偏好与问题无关，但重要性高，两次不同提问都该在前列。"""
    store = build(now)
    for query in ("VPN 认证失败", "打印机卡纸"):
        hits = store.recall(query, user_id="u-1", now=now, top_k=3)
        assert any(row.item.kind == KIND_SEMANTIC for row in hits)


def test_recall_is_ordered_by_score(now: float) -> None:
    store = build(now)
    hits = store.recall("VPN 认证失败", user_id="u-1", now=now, top_k=5)
    scores = [row.score for row in hits]
    assert scores == sorted(scores, reverse=True)


def test_recall_respects_user_isolation(now: float) -> None:
    store = build(now)
    other = store.remember(KIND_EPISODIC, "李娜的工单：邮箱配额已满",
                           user_id="u-2", now=now - DAY, importance=0.9)

    hits = store.recall("工单", user_id="u-1", now=now, top_k=5)
    assert all(row.item.user_id == "u-1" for row in hits)
    assert other.id not in {row.item.id for row in hits}


def test_kinds_filter(now: float) -> None:
    store = build(now)
    only_episodic = store.recall("VPN", user_id="u-1", now=now, top_k=5,
                                 kinds=[KIND_EPISODIC])
    assert all(row.item.kind == KIND_EPISODIC for row in only_episodic)


def test_unknown_kind_rejected(now: float) -> None:
    with pytest.raises(ValueError):
        MemoryStore().remember("guess", "x", user_id="u-1", now=now)


# ------------------------------ 衰减 ------------------------------


def _score_at(now: float, age_days: float, importance: float = 0.5) -> float:
    store = MemoryStore()
    store.remember(KIND_EPISODIC, "VPN 认证失败，重置密码后恢复",
                   user_id="u", now=now - age_days * DAY, importance=importance)
    return store.recall("VPN 认证失败", user_id="u", now=now, top_k=1)[0].score


def test_older_memory_scores_lower(now: float) -> None:
    assert _score_at(now, 1) > _score_at(now, 90) > _score_at(now, 365)


def test_importance_lifts_old_memory(now: float) -> None:
    assert _score_at(now, 90, importance=0.9) > _score_at(now, 90, importance=0.3)


# ------------------------------ 遗忘 ------------------------------


def test_forget_removes_item(now: float) -> None:
    store = build(now)
    target = store.recall("打印机卡纸", user_id="u-1", now=now, top_k=1)[0].item

    assert store.forget(target.id) is True
    hits = store.recall("打印机卡纸", user_id="u-1", now=now, top_k=5)
    assert target.id not in {row.item.id for row in hits}


def test_forget_unknown_returns_false() -> None:
    assert MemoryStore().forget("mem_nope") is False


def test_forget_expired_cleans_ttl_items(now: float) -> None:
    store = build(now)
    assert store.stats(now)["expired"] == 1

    removed = store.forget_expired(now)
    assert removed == 1
    assert store.stats(now)["expired"] == 0


def test_forget_user_removes_everything(now: float) -> None:
    store = build(now)
    removed = store.forget_user("u-1")

    assert removed == 4
    assert store.recall("VPN", user_id="u-1", now=now, top_k=5) == []


# ------------------------------ 上下文块 ------------------------------


def test_context_block_labels_kinds(now: float) -> None:
    store = build(now)
    hits = store.recall("VPN 认证失败", user_id="u-1", now=now, top_k=3)
    block = store.context_block(hits, now=now)

    assert "[历史·90 天前]" in block
    assert "[偏好·" in block
    assert block.count("\n") == len(hits) - 1


def test_stats_shape(now: float) -> None:
    stats = build(now).stats(now)
    assert stats["total"] == 4
    assert stats["semantic"] == 1
    assert stats["episodic"] == 3
    assert stats["expired"] == 1
