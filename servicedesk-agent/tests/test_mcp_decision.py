# file: tests/test_mcp_decision.py
"""2.2 的断言：决策四问的结论必须可复现、可解释。"""

from __future__ import annotations

import pytest

from servicedesk.tools.mcp_decision import (
    PROTOCOL_OVERHEAD,
    SCENARIOS,
    Scenario,
    decide,
)


def test_adapter_math() -> None:
    """适配器数量：各自定制 M×N，统一协议 M+N+协议成本。"""
    decision = decide(Scenario(key="t", title="t", consumers=5, systems=5))
    assert decision.adapters_custom == 25
    assert decision.adapters_mcp == 5 + 5 + PROTOCOL_OVERHEAD
    assert decision.savings == 25 - 11


def test_single_consumer_never_suggests_mcp() -> None:
    """M=1 时不管 N 多大都不划算 —— 这是最常见的误判。"""
    for systems in (0, 1, 3, 10):
        decision = decide(Scenario(key="t", title="t", consumers=1, systems=systems))
        assert decision.suggest is False, f"N={systems} 时不该建议 MCP"


def test_many_consumers_and_systems_suggests_mcp() -> None:
    decision = decide(Scenario(key="t", title="t", consumers=5, systems=5))
    assert decision.suggest is True
    assert decision.savings > 0
    assert "同时成立" in decision.reasons[-1]


def test_break_even_needs_business_reason() -> None:
    """3×2 在算数上打平；没有业务理由时不该建议。"""
    plain = decide(Scenario(key="t", title="t", consumers=3, systems=2))
    assert plain.adapters_custom == plain.adapters_mcp
    assert plain.suggest is False

    with_isolation = decide(
        Scenario(key="t", title="t", consumers=3, systems=2, needs_isolation=True)
    )
    assert with_isolation.suggest is True
    assert "业务理由" in with_isolation.verdict


def test_third_party_overrides_arithmetic() -> None:
    """跨组织接入时，算数不占优也必须用协议。"""
    decision = decide(
        Scenario(key="t", title="t", consumers=2, systems=1, third_party=True)
    )
    assert decision.savings < 0
    assert decision.suggest is True
    assert "压倒算数" in decision.reasons[-1]


def test_isolation_alone_with_single_consumer_is_not_enough() -> None:
    decision = decide(
        Scenario(key="t", title="t", consumers=1, systems=3, needs_isolation=True)
    )
    assert decision.suggest is False


def test_reasons_cover_all_four_questions() -> None:
    decision = decide(Scenario(key="t", title="t", consumers=2, systems=2))
    joined = " ".join(decision.reasons)
    for marker in ("问1", "问2", "问3", "问4", "结论"):
        assert marker in joined


def test_preset_scenarios_match_documented_verdicts() -> None:
    """docs/mcp-notes.md §5 里写的三个结论，代码必须一致。"""
    by_key = {scenario.key: scenario for scenario in SCENARIOS}

    assert decide(by_key["A"]).suggest is True      # 工单查询：业务理由优先
    assert decide(by_key["B"]).suggest is False     # 格式化日期：不该做
    assert decide(by_key["C"]).suggest is True      # 合作方资产查询：跨组织必须做
    assert decide(by_key["D"]).suggest is False     # 单消费方多系统：仍不该做
    assert decide(by_key["E"]).suggest is True      # 5×5：算数与业务双赢


@pytest.mark.parametrize("consumers,systems", [(0, 0), (1, 0), (0, 3)])
def test_degenerate_inputs_do_not_crash(consumers: int, systems: int) -> None:
    decision = decide(Scenario(key="t", title="t", consumers=consumers, systems=systems))
    assert decision.suggest is False
