# file: tests/test_short_term_memory.py
"""4.4 的断言：token 估算、约束抽取、三种策略的取舍、压缩触发。"""

from __future__ import annotations

import pytest

from servicedesk.memory.short_term import (
    BudgetedHistory,
    Message,
    estimate_tokens,
    extract_constraints,
    pin_constraints,
)


# ------------------------------ token 估算 ------------------------------


def test_estimate_tokens_grows_with_length() -> None:
    short = estimate_tokens("你好")
    long = estimate_tokens("你好你好你好你好")
    assert long > short >= 1


def test_estimate_tokens_empty_is_zero() -> None:
    assert estimate_tokens("") == 0


def test_cjk_costs_more_than_ascii() -> None:
    """同样字符数，中文比英文贵——这是预算要用 token 算的原因。"""
    assert estimate_tokens("中文中文中文中文") > estimate_tokens("abcdabcdabcdabcd")


# ------------------------------ 约束抽取 ------------------------------

def _messages(*contents: str) -> list[Message]:
    return [Message("user", text) for text in contents]


def test_extract_ticket_id() -> None:
    found = extract_constraints(_messages("我的工单号是 T-1001。"))
    assert found == [type(found[0])("ticket", "T-1001")]


def test_extract_prohibition() -> None:
    found = extract_constraints(_messages("注意：不要重置我的密码，我有重要会议。"))
    assert any(item.kind == "prohibit" and "不要重置" in item.text for item in found)


def test_would_rather_not_but_must_not_is_not_prohibition() -> None:
    """回归：第一版用 `"不要" in text`，把「要不要」也当成禁令。"""
    found = extract_constraints(_messages("VPN 要不要额外配置？"))
    assert found == []


def test_extract_must() -> None:
    found = extract_constraints(_messages("这个必须今天处理完。"))
    assert any(item.kind == "must" and "必须" in item.text for item in found)


def test_constraints_are_deduplicated() -> None:
    found = extract_constraints(_messages("工单 T-1001", "再说一次，工单 T-1001"))
    assert [item for item in found if item.kind == "ticket"] == [type(found[0])("ticket", "T-1001")]


def test_pin_marks_constraint_messages() -> None:
    messages = pin_constraints(_messages("工单 T-1001", "今天天气不错", "不要重置密码"))
    assert [message.pinned for message in messages] == [True, False, True]


# ------------------------------ 三种策略 ------------------------------


def build_conversation() -> BudgetedHistory:
    history = BudgetedHistory(budget_tokens=300, keep_recent=3)
    for index in range(3):
        history.add("user", f"第{index + 1}个无关问题：打印机在第{index + 1}层怎么连？")
        history.add("assistant", f"第{index + 1}层的打印机要先装驱动再添加端口。")
    history.add("user", "我的工单号是 T-1001，后面请用它称呼。")
    history.add("assistant", "收到，工单号 T-1001。")
    history.add("user", "注意：不要重置我的密码，我有重要会议。")
    history.add("assistant", "明白，不会重置密码。")
    for index in range(3):
        history.add("user", f"再问：VPN 在第{index + 1}个办公区要不要额外配置？")
        history.add("assistant", f"第{index + 1}个办公区不需要额外配置。")
    history.add("user", "帮我总结一下现在的情况。")
    history.pin_all_constraints()
    return history


def test_window_loses_constraints() -> None:
    result = build_conversation().compress("window")
    lost_kinds = {item.kind for item in result.lost_constraints}
    assert "ticket" in lost_kinds
    assert "prohibit" in lost_kinds


def test_summary_also_loses_constraints() -> None:
    """摘要器不知道什么重要——贵了 3 倍 token，丢的东西一样多。"""
    result = build_conversation().compress("summary")
    lost_kinds = {item.kind for item in result.lost_constraints}
    assert "ticket" in lost_kinds
    assert "prohibit" in lost_kinds


def test_hybrid_keeps_all_constraints() -> None:
    result = build_conversation().compress("hybrid")
    assert result.lost_constraints == []


def test_hybrid_costs_more_than_window() -> None:
    window = build_conversation().compress("window")
    hybrid = build_conversation().compress("hybrid")
    assert hybrid.tokens_after > window.tokens_after


def test_all_strategies_reduce_tokens() -> None:
    for strategy in ("window", "summary", "hybrid"):
        result = build_conversation().compress(strategy)
        assert result.tokens_after < result.tokens_before
        assert result.dropped > 0


def test_hybrid_without_pinning_degrades_to_window() -> None:
    """忘了 pin，hybrid 也会丢约束——这是本节实测的坑。"""
    history = build_conversation()
    for message in history.messages:
        message.pinned = False
    result = history.compress("hybrid")
    assert result.lost_constraints != []


# ------------------------------ 压缩触发 ------------------------------


def test_over_budget_detects_exceeded() -> None:
    history = BudgetedHistory(budget_tokens=20, keep_recent=2)
    history.add("user", "这是一句很长很长的话，用来把预算撑爆；再加一些字确保一定超过二十个 token。")
    assert history.over_budget(), f"实际 {history.tokens()} tokens"


def test_compress_brings_multi_message_history_under_budget() -> None:
    history = BudgetedHistory(budget_tokens=60, keep_recent=2)
    for index in range(6):
        history.add("user", f"第{index + 1}条消息，内容不算短，用来把预算慢慢撑大。")
    assert history.over_budget()

    history.compress("window")
    assert not history.over_budget()


def test_single_oversized_message_cannot_be_compressed() -> None:
    """已知限制：单条就超预算的消息（比如超长工具返回），裁剪策略救不了它。

    必须先做**单条截断**（或把大结果落盘、只留摘要引用），否则永远超预算。
    本项目把这个限制留在文档里，而不是假装它不存在。
    """
    history = BudgetedHistory(budget_tokens=20, keep_recent=2)
    history.add("user", "这是一条本身就超过预算的超长消息。" * 5)
    history.compress("hybrid")
    assert history.over_budget(), "单条超长消息无法靠裁剪解决"


def test_compress_keeps_recent_messages() -> None:
    history = BudgetedHistory(budget_tokens=10, keep_recent=2)
    history.add("user", "第一条消息，内容很长很长。")
    history.add("user", "第二条消息，内容很长很长。")
    history.add("user", "第三条最近的消息")
    result = history.compress("window")

    assert result.kept[-1].content == "第三条最近的消息"


def test_compress_is_idempotent_on_short_history() -> None:
    history = BudgetedHistory(budget_tokens=1000, keep_recent=5)
    history.add("user", "短会话")
    result = history.compress("hybrid")
    assert result.dropped == 0
    assert len(history.messages) == 1
