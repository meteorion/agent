# file: tests/test_launch_checklist.py
"""5.8 的断言：自动检查全绿、人工项必须签字、演练能复现阻塞。"""

from __future__ import annotations

import pytest

from servicedesk.ops.launch_checklist import (
    MANUAL_CHECKS,
    Check,
    ChecklistReport,
    Kind,
    Severity,
    build_checklist,
    render,
)

ALL_MANUAL = {check.id for check in MANUAL_CHECKS}


@pytest.fixture(scope="module")
def auto_report() -> ChecklistReport:
    report = ChecklistReport()
    report.run()
    return report


@pytest.fixture(scope="module")
def signed_report() -> ChecklistReport:
    report = ChecklistReport()
    report.run(manual_ack=ALL_MANUAL)
    return report


# ------------------------------ 清单结构 ------------------------------


def test_checklist_has_both_kinds() -> None:
    checks = build_checklist()
    kinds = [check.kind for check in checks]
    assert kinds.count(Kind.AUTO) == 10
    assert kinds.count(Kind.MANUAL) == 6


def test_every_check_has_unique_id() -> None:
    ids = [check.id for check in build_checklist()]
    assert len(ids) == len(set(ids))


def test_auto_checks_all_have_runners() -> None:
    for check in build_checklist():
        if check.kind is Kind.AUTO:
            assert check.runner is not None, f"{check.id} 缺少 runner"


def test_manual_checks_have_no_runner() -> None:
    for check in build_checklist():
        if check.kind is Kind.MANUAL:
            assert check.runner is None


# ------------------------------ 自动检查真的在跑 ------------------------------


def test_all_auto_checks_pass(auto_report: ChecklistReport) -> None:
    """当前仓库状态下，10 项自动检查应全部通过。"""
    auto_failures = [
        result.as_dict()
        for result in auto_report.results
        if result.check.kind is Kind.AUTO and not result.passed
    ]
    assert auto_failures == [], f"自动检查失败：{auto_failures}"


def test_auto_only_cannot_launch(auto_report: ChecklistReport) -> None:
    """自动全绿也不行——人工项没签字。"""
    assert auto_report.passed() is False
    assert len(auto_report.blockers()) == 6


def test_signed_report_passes(signed_report: ChecklistReport) -> None:
    assert signed_report.passed() is True
    assert signed_report.blockers() == []
    assert len([r for r in signed_report.results if r.passed]) == 16


# ------------------------------ 演练 ------------------------------


def test_override_blocks_launch() -> None:
    report = ChecklistReport()
    report.run(
        manual_ack=ALL_MANUAL,
        overrides={"eval.gate": (False, "回退指标：['agent.authorization']")},
    )
    assert report.passed() is False
    blockers = report.blockers()
    assert len(blockers) == 1
    assert blockers[0].check.id == "eval.gate"
    assert "agent.authorization" in blockers[0].detail


def test_override_can_force_pass() -> None:
    report = ChecklistReport()
    report.run(
        overrides={"guardrails.no_false_positive": (True, "人工认定可接受")},
    )
    passed = {result.check.id for result in report.results if result.passed}
    assert "guardrails.no_false_positive" in passed


def test_manual_ack_ignores_unknown_ids() -> None:
    report = ChecklistReport()
    report.run(manual_ack={"manual.nonexistent"})
    assert report.passed() is False


# ------------------------------ 异常与渲染 ------------------------------


def test_runner_exception_counts_as_failure() -> None:
    def boom() -> tuple[bool, str]:
        raise RuntimeError("检查自己炸了")

    result = Check("x", "test", "q", Kind.AUTO, runner=boom).run()
    assert result[0] is False
    assert "检查执行失败" in result[1]


def test_render_shows_blockers(auto_report: ChecklistReport) -> None:
    text = render(auto_report)
    assert "❌ 不许上线" in text
    assert "阻塞项" in text
    assert "manual.rollback" in text


def test_render_shows_pass(signed_report: ChecklistReport) -> None:
    text = render(signed_report)
    assert "✅ 可以上线" in text
    assert "阻塞项：" not in text, "通过时不该出现阻塞项列表"
    assert "0 个阻塞项" in text


def test_report_as_dict_shape(signed_report: ChecklistReport) -> None:
    data = signed_report.as_dict()
    assert data["passed"] is True
    assert data["total"] == 16
    assert data["passed_count"] == 16
    assert data["blockers"] == []
    assert len(data["results"]) == 16


def test_by_category_groups_all_results(signed_report: ChecklistReport) -> None:
    groups = signed_report.by_category()
    assert sum(len(items) for items in groups.values()) == 16
    assert "评测" in groups and "安全" in groups


def test_severity_defaults_to_blocker() -> None:
    assert Check("x", "c", "q", Kind.AUTO).severity is Severity.BLOCKER
