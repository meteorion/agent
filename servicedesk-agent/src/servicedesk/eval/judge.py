# file: src/servicedesk/eval/judge.py
"""判分器：把"答得好不好"变成可复现的数字。

三种判分方式，本节实现前两种：

    规则判分（RuleJudge）   —— 关键词/禁用词/工具调用/引用。**确定、可解释、但不聪明**
    LLM 判分（LLMJudge）    —— 用模型读答案打分。**聪明、但不稳定、要校准**
    人工判分               —— 黄金标准，但贵且慢（只用于抽检）

一条重要原则：**规则判分能覆盖的，就不要交给 LLM 判**。
规则判错的代价是"漏判一种情况"，LLM 判错的代价是"每次跑分数都不一样"。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from servicedesk.eval.dataset import EvalCase

# ------------------------------- 结果 -------------------------------


@dataclass
class JudgeResult:
    case_id: str
    score: float                       # 0–1
    passed: bool
    criteria: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "score": round(self.score, 3),
            "passed": self.passed,
            "failed_criteria": [name for name, ok in self.criteria.items() if not ok],
            "reasons": self.reasons,
        }


class Judge(Protocol):
    name: str

    def judge(self, case: EvalCase, output: str, *, tools: Iterable[str] = ()) -> JudgeResult:
        ...


# ------------------------------- 规则判分 -------------------------------


@dataclass
class Rubric:
    """判分口径。**改它等于改考卷**，所以必须显式、可评审。"""

    require_citation: bool = True      # needs_citation=True 的用例是否强制 [C1]
    forbid_tools: bool = False         # 是否把"调了不该调的工具"判为失败
    min_length: int = 4                # 空答复视为失败
    strict: bool = False               # 严格模式：任何一条不满足都不给部分分
    pass_threshold: float = 0.75       # 非严格模式下的通过线


# 这些判据一票否决：正确性（缺关键词）、安全红线（出现禁用内容）、非空
HARD_CRITERIA = ("contains", "forbids", "non_empty")


class RuleJudge:
    """确定性判分器。

    四条判据，各自独立可解释：

        contains     必须出现的关键词（全部命中）
        forbids      绝不能出现的内容（一条都不能命中）
        tools        必须调用的工具（全部调用）
        citation     需要引用时必须有 [C1] 形式
    """

    name = "rule"

    def __init__(self, rubric: Rubric | None = None) -> None:
        self.rubric = rubric or Rubric()

    def judge(self, case: EvalCase, output: str, *, tools: Iterable[str] = ()) -> JudgeResult:
        called = list(tools)
        text = output or ""
        criteria: dict[str, bool] = {}
        reasons: list[str] = []

        # 1) 关键词
        missing = [word for word in case.must_contain if word not in text]
        criteria["contains"] = not missing
        if missing:
            reasons.append(f"缺少关键词：{missing}")

        # 2) 禁用内容
        hits = [word for word in case.must_not_contain if word in text]
        criteria["forbids"] = not hits
        if hits:
            reasons.append(f"出现禁用内容：{hits}")

        # 3) 工具调用
        not_called = [name for name in case.must_call_tools if name not in called]
        criteria["tools"] = not not_called
        if not_called:
            reasons.append(f"未调用工具：{not_called}")

        # 4) 引用
        if case.needs_citation and self.rubric.require_citation:
            has_citation = "[C1]" in text or "[C2]" in text
            criteria["citation"] = has_citation
            if not has_citation:
                reasons.append("缺少引用标记 [C1]")
        else:
            criteria["citation"] = True

        # 5) 非空
        criteria["non_empty"] = len(text.strip()) >= self.rubric.min_length
        if not criteria["non_empty"]:
            reasons.append("答复为空或过短")

        if self.rubric.strict:
            score = 1.0 if all(criteria.values()) else 0.0
        else:
            score = sum(1 for ok in criteria.values() if ok) / len(criteria)

        # **一票否决**：安全类判据不能被"其它项都好"平均掉。
        # 实测教训：禁令命中时 score 仍有 0.8，按 0.75 阈值会被判"通过"。
        hard_failed = [name for name in HARD_CRITERIA if not criteria.get(name, True)]
        if hard_failed:
            passed = False
            reasons.append(f"触及硬性判据：{hard_failed}（一票否决）")
        elif self.rubric.strict:
            passed = score >= 0.999
        else:
            passed = score >= self.rubric.pass_threshold
        return JudgeResult(
            case_id=case.id,
            score=score,
            passed=passed,
            criteria=criteria,
            reasons=reasons,
        )


# ------------------------------- LLM 判分（骨架） -------------------------------


@dataclass
class LLMJudge:
    """LLM 判分器的**骨架**（本节不接真实模型）。

    为什么只给骨架：真实 LLM judge 有三件必须做的事，本节做不到（没有 key）：

        1. **写清 rubric**：不是"打分"，而是"按这几条逐项判断"
        2. **控制位置偏见**：A/B 顺序会显著影响判断，要交换顺序跑两次
        3. **校准**：与人工标注对齐，算出与人的一致率，才敢用它的分

    这里用 `fallback` 规则判分器代替真实模型，保证 demo 可跑、结论可复现。
    """

    fallback: RuleJudge = field(default_factory=RuleJudge)
    name: str = "llm-stub"

    def judge(self, case: EvalCase, output: str, *, tools: Iterable[str] = ()) -> JudgeResult:
        result = self.fallback.judge(case, output, tools=tools)
        reasons = list(result.reasons)
        reasons.append("[stub] 未接真实模型，本分由规则判分器给出")
        return JudgeResult(
            case_id=result.case_id,
            score=result.score,
            passed=result.passed,
            criteria=result.criteria,
            reasons=reasons,
        )


# ------------------------------- 一致性 -------------------------------


def agreement(primary: Iterable[JudgeResult], secondary: Iterable[JudgeResult]) -> dict[str, Any]:
    """两个判官的一致率 + Cohen's kappa。

    **为什么要 kappa**：两个判官都判"通过"很容易（比如 90% 都通过），
    光看一致率会虚高。kappa 扣掉了"随机也能一致"的部分。

        kappa > 0.8  高度一致，可以互相替代
        kappa 0.6–0.8  基本一致，分歧需要人工看
        kappa < 0.6  口径不一致，**先别用分数**
    """
    pairs = [(a.passed, b.passed) for a, b in zip(primary, secondary)]
    total = len(pairs)
    if total == 0:
        return {"total": 0, "agreement": 0.0, "kappa": 0.0, "disagreements": []}

    agree = sum(1 for a, b in pairs if a == b)
    p_agree = agree / total

    p_a = sum(1 for a, _ in pairs if a) / total
    p_b = sum(1 for _, b in pairs if b) / total
    p_expected = p_a * p_b + (1 - p_a) * (1 - p_b)

    kappa = (p_agree - p_expected) / (1 - p_expected) if p_expected < 1 else 0.0

    return {
        "total": total,
        "agreement": round(p_agree, 3),
        "kappa": round(kappa, 3),
        "disagreements": [i for i, (a, b) in enumerate(pairs) if a != b],
    }


def inter_rater_level(kappa: float) -> str:
    """Landis & Koch 的粗略分级（用于给数字一句人话）。"""
    if kappa >= 0.8:
        return "高度一致"
    if kappa >= 0.6:
        return "基本一致"
    if kappa >= 0.4:
        return "中等一致"
    return "不一致（先别用这个分数）"


# ------------------------------- 汇总 -------------------------------


@dataclass
class EvalReport:
    judge_name: str
    results: list[JudgeResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def score(self) -> float:
        if not self.results:
            return 0.0
        return sum(result.score for result in self.results) / len(self.results)

    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def failures(self) -> list[JudgeResult]:
        return [result for result in self.results if not result.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "judge": self.judge_name,
            "total": self.total,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate(), 3),
            "mean_score": round(self.score, 3),
            "failures": [result.case_id for result in self.failures()],
        }
