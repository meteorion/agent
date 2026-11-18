# file: scripts/eval_dataset_demo.py
"""5.1 的场景：构造 20 条评测集、跑基线、看判官一致性、演示腐坏检测。

用法：
    uv run python -m scripts.eval_dataset_demo
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from servicedesk.eval.baseline import BaselineAgent
from servicedesk.eval.dataset import EvalCase, Dataset, build_default_dataset
from servicedesk.eval.judge import (
    EvalReport,
    JudgeResult,
    RuleJudge,
    Rubric,
    agreement,
    inter_rater_level,
)


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _dwidth(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _dwidth(text))


def demo_dataset(dataset: Dataset) -> None:
    header("第 1 步：20 条评测集长什么样")
    stats = dataset.stats()

    print(f"  总数: {stats['total']}")
    print(f"  切分: {stats['by_split']}")
    print(f"  难度: {stats['by_difficulty']}")
    print(f"  分类: {stats['by_tag']}")
    print()
    print("  前 6 条（id / 问题 / 判分要点）：")
    for case in dataset.cases[:6]:
        checks = []
        if case.must_contain:
            checks.append(f"含{case.must_contain}")
        if case.must_not_contain:
            checks.append(f"禁{case.must_not_contain}")
        if case.must_call_tools:
            checks.append(f"调{case.must_call_tools}")
        if case.needs_citation:
            checks.append("需引用")
        question = case.question or "（空输入）"
        print(f"    {pad(case.id, 11)}{pad(question[:22], 26)}{' '.join(checks)}")

    print()
    print("  >>> 注意 hard 用例占 8 条（40%）：它们大多是**期望失败**的用例")
    print("      （越权、承诺、注入、空输入）。没有它们，评测集只会告诉你「一切正常」。")


def demo_health(dataset: Dataset) -> None:
    header("第 2 步：数据集体检")
    problems = dataset.validate()
    audit = dataset.audit()

    print(f"  结构校验: {'通过' if not problems else problems}")
    print(f"  重复问题: {audit['duplicate_questions'] or '无'}")
    print(f"  dev/test 泄漏: {audit['dev_test_leaks'] or '无'}")
    print(f"  样本不足的分桶: {audit['thin_tags'] or '无'}")
    print(f"  结论: {'干净' if audit['clean'] else '有腐坏'}")
    print()
    print("  >>> 注意 thin_tags 是**提醒不是错误**：分桶只有 1 条时，")
    print("      该分桶的分数没有统计意义（一条用例的波动就是 ±100%）。")

    # 演示腐坏：加一条 dev/test 泄漏
    leaked = Dataset(cases=list(dataset.cases) + [
        EvalCase(
            id="vpn-01-copy", question="VPN 认证失败怎么办？",
            must_contain=["密码"], tags=["vpn"], difficulty="easy", split="test",
        )
    ])
    leaked_audit = leaked.audit()
    print()
    print("  人为制造一条泄漏（把 dev 的问题复制进 test）：")
    print(f"    泄漏检出: {leaked_audit['dev_test_leaks']}")
    print(f"    结论: {'干净' if leaked_audit['clean'] else '有腐坏 ✅ 被抓到了'}")


def demo_baseline(dataset: Dataset) -> tuple[EvalReport, list[JudgeResult]]:
    header("第 3 步：跑基线 —— 20 条用例，规则判分")

    agent = BaselineAgent()
    judge = RuleJudge()
    report = EvalReport(judge_name=judge.name)

    for case in dataset:
        output = agent.answer(case)
        report.results.append(judge.judge(case, output, tools=agent.tools_for(case)))

    data = report.as_dict()
    print(f"  判官      : {data['judge']}")
    print(f"  通过      : {data['passed']}/{data['total']}  ({data['pass_rate']:.0%})")
    print(f"  平均分    : {data['mean_score']:.3f}")
    print()

    print("  失败用例与原因：")
    for result in report.failures():
        print(f"    {pad(result.case_id, 11)}{pad(f'{result.score:.2f}', 6)}"
              f"{'；'.join(result.reasons)[:60]}")

    print()
    print(f"  >>> 注意 mean_score {data['mean_score']:.2f} 但 pass_rate 只有 {data['pass_rate']:.0%} ——")
    print("      **平均分会把安全失败稀释掉**。所以本节给 forbids/non_empty 加了一票否决。")
    return report, report.results


def demo_agreement(results: list[JudgeResult], dataset: Dataset) -> None:
    header("第 4 步：换一个通过线，分数就变了 —— 判官一致性")
    agent = BaselineAgent()

    lenient = RuleJudge(Rubric(pass_threshold=0.75))
    strict = RuleJudge(Rubric(pass_threshold=0.95))

    a: list[JudgeResult] = []
    b: list[JudgeResult] = []
    for case in dataset:
        output = agent.answer(case)
        tools = agent.tools_for(case)
        a.append(lenient.judge(case, output, tools=tools))
        b.append(strict.judge(case, output, tools=tools))

    report_a = EvalReport("lenient(0.75)", a)
    report_b = EvalReport("strict(0.95)", b)
    print(f"  宽松判官（通过线 0.75）: {report_a.passed}/{report_a.total} = {report_a.pass_rate():.0%}")
    print(f"  严格判官（通过线 0.95）: {report_b.passed}/{report_b.total} = {report_b.pass_rate():.0%}")

    stats = agreement(a, b)
    print()
    print(f"  一致率: {stats['agreement']:.0%}   Cohen's kappa: {stats['kappa']:.2f}"
          f"  → {inter_rater_level(stats['kappa'])}")
    print()
    print("  分歧用例（都是 4/5 分、恰好卡在两条线之间）：")
    for index in stats["disagreements"]:
        print(f"    {pad(a[index].case_id, 11)}score={a[index].score:.2f}  "
              f"宽松={a[index].passed}  严格={b[index].passed}")

    print()
    print(f"  >>> 两个判官**用的是同一套规则**，只差一个通过线，"
          f"kappa 就只有 {stats['kappa']:.2f}（{inter_rater_level(stats['kappa'])}）。")
    print("      这说明：**通过线这种看起来无害的参数，直接决定分数能不能用**。")
    print("      上线前必须先算一致率，再决定用哪个判官的分。")


def demo_persistence(dataset: Dataset) -> None:
    header("第 5 步：评测集要能存盘、能进 git")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "golden.jsonl"
        dataset.save(path)

        lines = path.read_text(encoding="utf-8").splitlines()
        reloaded = Dataset.load(path)

        print(f"  写入: {path.name}（{len(lines)} 行，每行一条用例）")
        print(f"  回读: {len(reloaded)} 条，首条 id={reloaded.cases[0].id}")
        print()
        print("  首行的样子：")
        first = json.loads(lines[0])
        for key in ("id", "question", "must_contain", "tags", "difficulty", "split"):
            print(f"    {pad(key, 16)}{first.get(key)}")
        print()
        print("  >>> 用 JSONL 而不是 JSON 数组：**一行一条，code review 能看到 diff**。")
        print("      一个巨大的 JSON 数组，改一条会导致整文件 diff，评审时看不清。")


def demo_checklist() -> None:
    header("第 6 步：建评测集的自检清单")
    checks = [
        ("每条用例都有明确的判分标准吗", "没有标准的用例 = 只能人工看 = 不可复现"),
        ("有没有『期望失败』的用例", "全是用例都会过的集合，区分度为零"),
        ("dev / test 分开了吗", "调参用 test 集 = 过拟合"),
        ("每个分桶够 2 条吗", "1 条用例的波动就是 ±100%"),
        ("hard 用例占比多少", "本节 40%——高是因为刻意塞了安全问题"),
        ("判分器能判错吗", "用一条『不可能满足』的用例验证（4.1 的教训）"),
        ("两个判官的一致率是多少", "低于 0.6 先别信分数"),
    ]
    for index, (question, why) in enumerate(checks, 1):
        print(f"  {index}. {question}")
        print(f"     └─ {why}")


def main() -> int:
    dataset = build_default_dataset()

    demo_dataset(dataset)
    demo_health(dataset)
    _, results = demo_baseline(dataset)
    demo_agreement(results, dataset)
    demo_persistence(dataset)
    demo_checklist()

    header("小结")
    print("  1. 评测集要有『期望失败』的用例，否则区分度为零。")
    print("  2. 安全类判据（forbids / 空答复）必须一票否决，不能被平均分稀释。")
    print("  3. 判官一致性要先算：同一套规则换个通过线，kappa 就掉到 0.6–0.8。")
    print("  4. 数据集要能存盘、能 review、能体检（重复 / 泄漏 / 覆盖不足）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
