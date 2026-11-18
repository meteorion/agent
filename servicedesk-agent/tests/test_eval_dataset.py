# file: tests/test_eval_dataset.py
"""5.1 的断言：数据集结构、体检、规则判分、一票否决、判官一致性。"""

from __future__ import annotations

from pathlib import Path

import pytest

from servicedesk.eval.baseline import BaselineAgent
from servicedesk.eval.dataset import EvalCase, Dataset, build_default_dataset
from servicedesk.eval.judge import (
    EvalReport,
    RuleJudge,
    Rubric,
    agreement,
    inter_rater_level,
)


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return build_default_dataset()


# ------------------------------ 数据集 ------------------------------


def test_default_dataset_has_20_cases(dataset: Dataset) -> None:
    assert len(dataset) == 20


def test_stats_breakdown(dataset: Dataset) -> None:
    stats = dataset.stats()
    assert stats["total"] == 20
    assert stats["by_split"]["dev"] + stats["by_split"]["test"] == 20
    assert sum(stats["by_difficulty"].values()) == 20
    assert stats["by_difficulty"]["hard"] >= 6, "必须有足够多的期望失败用例"


def test_dataset_has_negative_cases(dataset: Dataset) -> None:
    negative = [case for case in dataset if case.must_not_contain]
    assert len(negative) >= 5, "没有『期望失败』用例的评测集区分度为零"


def test_validate_passes_for_default_dataset(dataset: Dataset) -> None:
    assert dataset.validate() == []


def test_validate_catches_duplicate_id() -> None:
    dup = Dataset(cases=[
        EvalCase(id="x", question="a", must_contain=["a"]),
        EvalCase(id="x", question="b", must_contain=["b"]),
    ])
    assert any("重复 id" in problem for problem in dup.validate())


def test_validate_catches_missing_criteria() -> None:
    bad = Dataset(cases=[EvalCase(id="y", question="a")])
    assert any("没有任何判分标准" in problem for problem in bad.validate())


def test_audit_is_clean_for_default_dataset(dataset: Dataset) -> None:
    assert dataset.audit()["clean"] is True


def test_audit_reports_thin_tags(dataset: Dataset) -> None:
    thin = dataset.audit()["thin_tags"]
    assert "asset" in thin, "只有 1 条用例的分桶应该被提醒"


def test_audit_catches_dev_test_leak(dataset: Dataset) -> None:
    leaked = Dataset(cases=list(dataset.cases) + [
        EvalCase(id="vpn-01-copy", question="VPN 认证失败怎么办？",
                 must_contain=["密码"], split="test"),
    ])
    audit = leaked.audit()
    assert audit["dev_test_leaks"]
    assert audit["clean"] is False


def test_save_and_load_roundtrip(dataset: Dataset, tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    dataset.save(path)

    reloaded = Dataset.load(path)
    assert len(reloaded) == len(dataset)
    assert reloaded.cases[0].id == dataset.cases[0].id
    assert reloaded.cases[0].must_contain == dataset.cases[0].must_contain
    assert path.read_text(encoding="utf-8").count("\n") == len(dataset)


# ------------------------------ 判分器 ------------------------------


def _case(**kwargs) -> EvalCase:
    base = {"id": "t", "question": "q"}
    base.update(kwargs)
    return EvalCase(**base)  # type: ignore[arg-type]


def test_judge_passes_correct_answer() -> None:
    case = _case(must_contain=["密码"], needs_citation=True)
    result = RuleJudge().judge(case, "请先重置密码。[C1]")
    assert result.passed is True
    assert result.score == 1.0


def test_judge_fails_missing_keyword() -> None:
    case = _case(must_contain=["网盘"])
    result = RuleJudge().judge(case, "可以用别的方式发送。")
    assert result.passed is False
    assert "缺少关键词" in result.reasons[0]


def test_judge_fails_on_forbidden_content() -> None:
    case = _case(must_not_contain=["已开通"])
    result = RuleJudge().judge(case, "已开通 VPN 权限。")
    assert result.criteria["forbids"] is False


def test_forbidden_content_vetoes_even_with_high_score() -> None:
    """安全判据一票否决：4/5 分（0.8）也不能算通过。"""
    case = _case(must_contain=["权限"], must_not_contain=["已开通"])
    result = RuleJudge().judge(case, "已开通权限。")
    assert result.score >= 0.75
    assert result.passed is False
    assert any("一票否决" in reason for reason in result.reasons)


def test_judge_fails_missing_tool() -> None:
    case = _case(must_call_tools=["query_asset"])
    result = RuleJudge().judge(case, "NB-1024 是张伟在用。", tools=[])
    assert result.criteria["tools"] is False


def test_judge_passes_when_tool_called() -> None:
    case = _case(must_call_tools=["query_asset"])
    result = RuleJudge().judge(case, "NB-1024 是张伟在用。", tools=["query_asset"])
    assert result.criteria["tools"] is True


def test_judge_fails_empty_answer() -> None:
    case = _case(must_not_contain=["Traceback"])
    result = RuleJudge().judge(case, "")
    assert result.criteria["non_empty"] is False
    assert result.passed is False


def test_strict_mode_requires_all_criteria() -> None:
    case = _case(must_contain=["密码"], needs_citation=True)
    judge = RuleJudge(Rubric(strict=True))
    result = judge.judge(case, "请重置密码。")     # 缺引用
    assert result.score == 0.0
    assert result.passed is False


def test_reasons_are_explainable() -> None:
    case = _case(must_contain=["a", "b"], needs_citation=True)
    result = RuleJudge().judge(case, "只有 a")
    assert result.reasons, "失败必须给出可读原因"


# ------------------------------ 一致性 ------------------------------


def test_agreement_perfect_when_identical() -> None:
    judge = RuleJudge()
    mixed = [
        judge.judge(_case(id="ok1", must_contain=["密码"]), "请重置密码。"),
        judge.judge(_case(id="ok2", must_contain=["网盘"]), "请用企业网盘发送。"),
        judge.judge(_case(id="bad1", must_contain=["密码"]), "不知道。"),
        judge.judge(_case(id="bad2", must_contain=["网盘"]), "不知道。"),
    ]
    stats = agreement(mixed, mixed)
    assert stats["agreement"] == 1.0
    assert stats["kappa"] == 1.0


def test_agreement_detects_disagreement() -> None:
    judge = RuleJudge()
    good = judge.judge(_case(id="a", must_contain=["密码"]), "请重置密码。")
    bad = judge.judge(_case(id="b", must_contain=["密码"]), "")
    stats = agreement([good], [bad])
    assert stats["agreement"] == 0.0
    assert stats["disagreements"] == [0]


def test_kappa_penalises_trivial_agreement() -> None:
    """两边都判『全失败』时一致率 100%，但 kappa 不可用（分母为 0 → 记 0）。"""
    fails = [RuleJudge().judge(_case(id=f"f{i}", must_contain=["密码"]), "") for i in range(4)]
    stats = agreement(fails, fails)
    assert stats["agreement"] == 1.0
    assert stats["kappa"] == 0.0


@pytest.mark.parametrize(
    ("kappa", "expected"),
    [(0.9, "高度一致"), (0.7, "基本一致"), (0.5, "中等一致"), (0.2, "不一致")],
)
def test_inter_rater_level(kappa: float, expected: str) -> None:
    assert expected in inter_rater_level(kappa)


# ------------------------------ 基线与报告 ------------------------------


def test_baseline_is_imperfect_but_not_zero(dataset: Dataset) -> None:
    """基线要"能拿到一些分"也"丢掉一些分"，才有区分度。"""
    agent = BaselineAgent()
    judge = RuleJudge()
    report = EvalReport(judge_name="rule")
    for case in dataset:
        report.results.append(judge.judge(case, agent.answer(case), tools=agent.tools_for(case)))

    assert 0.2 < report.pass_rate() < 0.9


def test_baseline_report_numbers(dataset: Dataset) -> None:
    agent = BaselineAgent()
    judge = RuleJudge()
    report = EvalReport(judge_name="rule")
    for case in dataset:
        report.results.append(judge.judge(case, agent.answer(case), tools=agent.tools_for(case)))

    data = report.as_dict()
    assert data["total"] == 20
    assert data["passed"] == 7
    assert data["pass_rate"] == 0.35
    assert len(data["failures"]) == 13


def test_mean_score_hides_safety_failures(dataset: Dataset) -> None:
    """平均分 0.83 高于通过线 0.75，但真实通过率只有 50%——这就是要一票否决的理由。"""
    agent = BaselineAgent()
    judge = RuleJudge()
    report = EvalReport(judge_name="rule")
    for case in dataset:
        report.results.append(judge.judge(case, agent.answer(case), tools=agent.tools_for(case)))

    assert report.score > 0.75
    assert report.pass_rate() < 0.6
