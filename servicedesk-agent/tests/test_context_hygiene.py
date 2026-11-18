# file: tests/test_context_hygiene.py
"""4.6 的断言：四种污染各被至少一种解法拦下，且叠加后 prompt 干净。"""

from __future__ import annotations

import pytest

from servicedesk.context.hygiene import (
    ALL_FILTERS,
    MIN_CONFIDENCE,
    SHARED_USER,
    ContextItem,
    assemble,
    assemble_naive,
    build_polluted_items,
)

NOW = 1_800_000_000.0
USER = "u-1001"


@pytest.fixture()
def items() -> list[ContextItem]:
    return build_polluted_items(NOW)


# ------------------------------ 污染现场 ------------------------------


def test_naive_prompt_contains_cross_user_leak(items) -> None:
    prompt = assemble_naive(items, now=NOW)
    assert "13800001234" in prompt, "裸拼会把别人的手机号带进来"


def test_naive_prompt_contains_both_contradictions(items) -> None:
    prompt = assemble_naive(items, now=NOW)
    assert "需要重新认证" in prompt
    assert "自动重连" in prompt
    assert "互相矛盾" not in prompt, "裸拼不会提示矛盾"


def test_naive_prompt_contains_superseded_policy(items) -> None:
    prompt = assemble_naive(items, now=NOW)
    assert "密码重置后立即生效" in prompt


# ------------------------------ 单个过滤器 ------------------------------


def test_namespace_blocks_other_user(items) -> None:
    _, report = assemble(items, user_id=USER, now=NOW, enabled={"namespace"})
    assert report.as_dict()["leaks_blocked"] == 1


def test_shared_items_are_not_treated_as_leaks(items) -> None:
    """policy / kb 的 user_id 是 "*"，属于公共知识，不该被隔离掉。"""
    prompt, _ = assemble(items, user_id=USER, now=NOW, enabled={"namespace"})
    assert "密码重置后" in prompt
    assert "VPN 掉线" in prompt


def test_authority_drops_unverified_source(items) -> None:
    _, report = assemble(items, user_id=USER, now=NOW, enabled={"authority"})
    assert any("unknown" in item for item in report.low_authority_dropped)


def test_confidence_drops_low_confidence_conclusion(items) -> None:
    prompt, report = assemble(items, user_id=USER, now=NOW, enabled={"confidence"})
    assert report.as_dict()["low_confidence_dropped"] == 1
    assert "无需确认身份" not in prompt


def test_version_drops_superseded_policy(items) -> None:
    prompt, report = assemble(items, user_id=USER, now=NOW, enabled={"version"})
    assert report.as_dict()["superseded_dropped"] == 1
    assert "5 分钟内生效" in prompt
    assert "立即生效。" not in prompt


def test_conflict_is_detected_when_both_versions_live(items) -> None:
    """只开冲突检测时，旧政策还在 → 会额外报一条 reset_policy 冲突。"""
    _, report = assemble(items, user_id=USER, now=NOW, enabled={"conflict"})
    topics = {topic for topic, _, _ in report.conflicts}
    assert "reset_policy" in topics
    assert "vpn_reauth" in topics


def test_stale_marks_old_entries(items) -> None:
    prompt, report = assemble(items, user_id=USER, now=NOW, enabled={"stale"})
    assert report.as_dict()["stale_annotated"] == 2
    assert "**可能过期**" in prompt


# ------------------------------ 叠加 ------------------------------


def test_all_filters_produce_clean_prompt(items) -> None:
    prompt, report = assemble(items, user_id=USER, now=NOW)
    data = report.as_dict()

    assert "13800001234" not in prompt, "跨用户内容必须消失"
    assert "无需确认身份" not in prompt, "低置信度结论必须消失"
    assert "8.8.8.8" not in prompt, "低权威来源必须消失"
    assert "立即生效。" not in prompt, "被取代的旧政策必须消失"
    assert "互相矛盾" in prompt, "冲突必须被显式标出"

    assert data["leaks_blocked"] == 1
    assert data["low_confidence_dropped"] == 1
    assert data["low_authority_dropped"] == 1
    assert data["superseded_dropped"] == 1
    assert data["conflicts"] == 1
    assert data["kept"] == 4


def test_kept_only_stale_and_conflicting_items(items) -> None:
    _, report = assemble(items, user_id=USER, now=NOW)
    sources = sorted(item.source for item in report.kept)
    assert sources == ["kb", "kb", "kb", "policy"]


def test_total_blocked_counts_all_drop_paths(items) -> None:
    _, report = assemble(items, user_id=USER, now=NOW)
    assert report.total_blocked == 4


def test_each_filter_alone_is_insufficient(items) -> None:
    """没有任何单个过滤器能把四类问题都解决——这就是要叠加的理由。"""
    for name in ALL_FILTERS:
        prompt, report = assemble(items, user_id=USER, now=NOW, enabled={name})
        clean = (
            "13800001234" not in prompt
            and "无需确认身份" not in prompt
            and "立即生效。" not in prompt
            and "8.8.8.8" not in prompt
        )
        assert not clean, f"`{name}` 单独开竟然清干净了？检查样例是否失效"


def test_min_confidence_threshold_is_reasonable() -> None:
    assert 0.0 < MIN_CONFIDENCE < 1.0
    assert SHARED_USER == "*"
