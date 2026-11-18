# file: tests/test_stress_test.py
"""4.7 的断言：50 轮后约束保留率与成本曲线。"""

from __future__ import annotations

import pytest

from servicedesk.memory.stress_test import (
    CONFIGS,
    CONSTRAINT_ROUNDS,
    Config,
    run_all,
    run_stress,
)

ROUNDS = 50


@pytest.fixture(scope="module")
def results():
    return {result.name: result for result in run_all(rounds=ROUNDS)}


# ------------------------------ 基本形状 ------------------------------


def test_all_configs_run(results) -> None:
    assert set(results) == {config.name for config in CONFIGS}
    for result in results.values():
        assert result.rounds == ROUNDS
        assert len(result.curve) == ROUNDS


def test_deterministic() -> None:
    """同样输入跑两遍结果一致——压测必须可复现。"""
    first = run_stress(Config(name="hybrid", budget=400), rounds=20)
    second = run_stress(Config(name="hybrid", budget=400), rounds=20)
    assert first.as_dict() == second.as_dict()


def test_constraints_are_deduplicated(results) -> None:
    """注入了 5 条约束消息，但第 47 轮重复确认 T-1001 → 抽取时去重成 4 个。"""
    assert len(CONSTRAINT_ROUNDS) == 5
    for result in results.values():
        assert len(result.constraints_injected) == 4


# ------------------------------ 成本 ------------------------------


def test_baseline_never_compresses(results) -> None:
    baseline = results["baseline"]
    assert baseline.compressions == 0
    assert baseline.final_tokens == baseline.peak_tokens


def test_baseline_is_most_expensive(results) -> None:
    baseline = results["baseline"]
    for name, result in results.items():
        if name == "baseline":
            continue
        assert result.cumulative_tokens < baseline.cumulative_tokens


def test_hybrid_saves_at_least_70_percent(results) -> None:
    """累计成本（每轮上下文之和）相比不压缩至少省 70%。"""
    baseline = results["baseline"].cumulative_tokens
    hybrid = results["hybrid"].cumulative_tokens
    assert 1 - hybrid / baseline > 0.70, f"只省了 {1 - hybrid / baseline:.0%}"


def test_window_is_cheaper_than_hybrid(results) -> None:
    assert results["window"].cumulative_tokens < results["hybrid"].cumulative_tokens


def test_baseline_curve_keeps_growing(results) -> None:
    curve = results["baseline"].curve
    samples = [curve[mark] for mark in (10, 20, 30, 40, 50)]
    assert samples == sorted(samples), "不压缩就该一路涨"


def test_compressed_curve_is_bounded(results) -> None:
    """压缩过的配置，峰值应该接近预算而不是无限涨。"""
    for name in ("window", "hybrid", "full"):
        assert results[name].peak_tokens <= 450, f"{name} 峰值 {results[name].peak_tokens}"


# ------------------------------ 约束保留 ------------------------------


def test_window_loses_all_constraints(results) -> None:
    """只留最近几条 = 把用户强调过的事全丢掉。"""
    window = results["window"]
    assert window.constraints_retained == []
    assert window.retention_rate == 0.0


def test_hybrid_retains_all_constraints(results) -> None:
    hybrid = results["hybrid"]
    assert len(hybrid.constraints_retained) == len(hybrid.constraints_injected)
    assert hybrid.retention_rate == 1.0


def test_baseline_also_retains_everything(results) -> None:
    """不压缩当然不丢——代价在钱上，不在正确性上。"""
    assert results["baseline"].retention_rate == 1.0


def test_hybrid_keeps_ticket_and_prohibition(results) -> None:
    retained = " ".join(results["hybrid"].constraints_retained)
    assert "T-1001" in retained
    assert "不要重置" in retained


# ------------------------------ 污染拦截 ------------------------------


def test_hygiene_blocks_some_pollution(results) -> None:
    assert results["hybrid"].pollution_blocked == 0, "没开卫生，一条都不拦"
    assert results["full"].pollution_blocked >= 2, "开了卫生，至少拦下跨用户与低权威"


def test_pollution_injection_count(results) -> None:
    for result in results.values():
        assert result.pollution_injected == ROUNDS // 15
