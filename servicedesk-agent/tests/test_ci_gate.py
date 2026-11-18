# file: tests/test_ci_gate.py
"""5.4 的断言：回退判定、缺失指标、退出码、真实 CLI 闭环。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from servicedesk.eval.ci_gate import (
    DEFAULT_RULES,
    MetricRule,
    collect_metrics,
    compare,
    dry_run_report,
    load_snapshot,
    render,
    save_snapshot,
)


@pytest.fixture(scope="module")
def healthy() -> dict[str, float]:
    return collect_metrics()


@pytest.fixture(scope="module")
def unhealthy() -> dict[str, float]:
    return collect_metrics(broken=True)


# ------------------------------ 判定逻辑 ------------------------------


def test_identical_metrics_pass(healthy: dict[str, float]) -> None:
    assert compare(healthy, healthy).passed is True


def test_floor_violation_blocks() -> None:
    current = {"x": 0.50}
    baseline = {"x": 0.50}
    decision = compare(current, baseline, [MetricRule("x", floor=0.90)])
    assert decision.passed is False
    assert "下限" in decision.regressions[0].reason


def test_drop_beyond_tolerance_blocks() -> None:
    decision = compare({"x": 0.80}, {"x": 0.95}, [MetricRule("x", max_drop=0.05)])
    assert decision.passed is False
    assert decision.regressions[0].delta == pytest.approx(-0.15)


def test_small_drop_within_tolerance_passes() -> None:
    decision = compare({"x": 0.93}, {"x": 0.95}, [MetricRule("x", max_drop=0.05)])
    assert decision.passed is True


def test_warning_severity_does_not_block() -> None:
    decision = compare({"x": 0.10}, {"x": 0.90},
                       [MetricRule("x", floor=0.50, severity="warn")])
    assert decision.passed is True
    assert decision.regressions, "仍然要记录回退，只是不拦"


def test_missing_metric_fails() -> None:
    decision = compare({"a": 1.0}, {"a": 1.0, "b": 1.0},
                       [MetricRule("a"), MetricRule("b")])
    assert decision.passed is False
    assert decision.missing == ["b"]


def test_severity_split_in_payload(unhealthy: dict[str, float], healthy: dict[str, float]) -> None:
    payload = compare(unhealthy, healthy).as_dict()
    assert payload["passed"] is False
    assert len(payload["blocking"]) >= 4
    assert all(item["delta"] < 0 for item in payload["blocking"])


# ------------------------------ 快照 ------------------------------


def test_snapshot_roundtrip(tmp_path: Path, healthy: dict[str, float]) -> None:
    path = tmp_path / "baseline.json"
    save_snapshot(path, healthy)
    loaded = load_snapshot(path)

    assert set(loaded) == set(healthy)
    assert loaded["agent.tool_order"] == pytest.approx(healthy["agent.tool_order"])
    assert path.read_text(encoding="utf-8").strip().startswith("{")


def test_snapshot_accepts_flat_json(tmp_path: Path) -> None:
    """也接受没有 `metrics` 包裹的扁平 JSON。"""
    path = tmp_path / "flat.json"
    path.write_text('{"x": 0.5}', encoding="utf-8")
    assert load_snapshot(path) == {"x": 0.5}


# ------------------------------ 指标收集 ------------------------------


def test_collect_metrics_has_all_rule_keys(healthy: dict[str, float]) -> None:
    for rule in DEFAULT_RULES:
        assert rule.name in healthy, f"缺少 {rule.name}"


def test_healthy_metrics_pass_all_rules(healthy: dict[str, float]) -> None:
    assert compare(healthy, healthy).passed is True


def test_broken_metrics_fail_gate(unhealthy: dict[str, float], healthy: dict[str, float]) -> None:
    decision = compare(unhealthy, healthy)
    assert decision.passed is False
    names = {item.name for item in decision.regressions}
    assert {"dataset.pass_rate", "agent.authorization", "agent.injection"} <= names


def test_dry_run_report_mentions_retrieval(unhealthy: dict[str, float]) -> None:
    """把指标交给 5.2 的诊断器，应能说出"检索太宽"。"""
    assert "检索" in dry_run_report(unhealthy)


# ------------------------------ 渲染 ------------------------------


def test_render_marks_block_and_warn(unhealthy, healthy) -> None:
    text = render(compare(unhealthy, healthy))
    assert "❌ FAIL" in text
    assert "[BLOCK]" in text


def test_render_pass_has_no_block(healthy) -> None:
    text = render(compare(healthy, healthy), healthy)
    assert "✅ PASS" in text
    assert "[BLOCK]" not in text


# ------------------------------ 真实 CLI ------------------------------


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "servicedesk.eval.ci_gate", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )


def test_cli_passes_with_matching_baseline(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    assert run_cli("--baseline", str(baseline), "--save-baseline").returncode == 0

    result = run_cli("--baseline", str(baseline))
    assert result.returncode == 0
    assert "PASS" in result.stdout


def test_cli_fails_when_broken(tmp_path: Path) -> None:
    """验收点：故意把指标打坏，流水线必须变红（退出码非 0）。"""
    baseline = tmp_path / "baseline.json"
    run_cli("--baseline", str(baseline), "--save-baseline")

    result = run_cli("--baseline", str(baseline), "--broken")
    assert result.returncode == 1
    assert "FAIL" in result.stdout
    assert "[BLOCK]" in result.stdout


def test_cli_missing_baseline_returns_2(tmp_path: Path) -> None:
    result = run_cli("--baseline", str(tmp_path / "nope.json"))
    assert result.returncode == 2
