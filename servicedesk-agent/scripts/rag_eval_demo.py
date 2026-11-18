# file: scripts/rag_eval_demo.py
"""5.2 的场景：给 RAG 打分，并从分数反推病根。

用法：
    uv run python -m scripts.rag_eval_demo
"""

from __future__ import annotations

from servicedesk.eval.rag_eval import (
    RagReport,
    RagSample,
    compare,
    diagnose,
    evaluate,
    score_context_precision,
    score_context_recall,
    score_faithfulness,
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


# ------------------------------- 语料 -------------------------------

F1 = "认证失败通常是密码过期，请先在自助门户重置密码。"
F2 = "重置后 5 分钟内生效，期间请勿重复提交。"
F3 = "单封邮件附件上限 25 MB，超出请用企业网盘分享链接。"
F3_SHORT = "单封邮件附件上限 25 MB。"
NOISE1 = "打印机驱动请到内网软件中心下载。"
NOISE2 = "会议室投屏请使用 HDMI 线。"

Q1 = "VPN 认证失败怎么办？"
GT1 = "请先在自助门户重置密码，重置后 5 分钟内生效。"
K1 = ["密码过期", "重置密码", "5 分钟内生效"]

Q2 = "附件太大发不出去怎么办？"
GT2 = "附件上限 25 MB，超出请用企业网盘分享链接。"
K2 = ["25 MB", "企业网盘"]


def build_variants() -> dict[str, list[RagSample]]:
    """四套 RAG 配置：一套健康，三套各坏在一个环节。"""
    return {
        "good": [
            RagSample(Q1, GT1, [F1, F2], "请先在自助门户重置密码，重置后 5 分钟内生效。", K1),
            RagSample(Q2, GT2, [F3], "附件上限 25 MB，超出请用企业网盘分享链接。", K2),
        ],
        "noisy": [     # 检索太宽：把无关文档也捞进来了
            RagSample(Q1, GT1, [F1, F2, NOISE1, NOISE2], "请先在自助门户重置密码，重置后 5 分钟内生效。", K1),
            RagSample(Q2, GT2, [F3, NOISE1, NOISE2], "附件上限 25 MB，超出请用企业网盘分享链接。", K2),
        ],
        "narrow": [    # 检索太窄：关键文档没被召回
            RagSample(Q1, GT1, [F1], "请先重置密码。", K1),
            RagSample(Q2, GT2, [F3_SHORT], "附件上限 25 MB。", K2),
        ],
        "hallucinating": [   # 生成幻觉：上下文是对的，答复自己编
            RagSample(Q1, GT1, [F1, F2], "认证失败请立刻联系厂商，他们会远程解锁账号。", K1),
            RagSample(Q2, GT2, [F3], "附件上限 100 MB，直接发送即可。", K2),
        ],
    }


# ------------------------------- 演示 -------------------------------


def demo_ragas_reality() -> None:
    header("第 1 步：先说实话 —— RAGAS 装得上，但离线跑不了")
    print("  实测安装：ragas 0.4.4（会带进 instructor / tiktoken / sqlalchemy 等依赖）")
    print("  实测 API：")
    print("    from ragas.metrics.collections import Faithfulness, AnswerRelevancy, ...")
    print("    Faithfulness.__init__(self, llm: InstructorBaseRagasLLM, ...)")
    print()
    print("  没有 API key 时：")
    print("    >>> Faithfulness()")
    print("    TypeError: Faithfulness.__init__() missing 1 required positional argument: 'llm'")
    print()
    print("  >>> 结论：RAGAS 的分数本质是**模型判断的结论**，没有模型就没有分数。")
    print("      所以本节实现四项**确定性代理指标**，并逐项标明与 RAGAS 的差异（见模块开头）。")
    print("      **本节的数字不能与公开榜单上的 RAGAS 分数对比。**")


def demo_single_metrics() -> None:
    header("第 2 步：四项指标各自在算什么")
    answer = "请先在自助门户重置密码，重置后 5 分钟内生效。"
    contexts = [F1, F2]

    print(f"  样本: 答复=「{answer}」")
    print(f"        上下文=2 条（密码过期 / 5 分钟生效）")
    print()
    print(f"  faithfulness      {score_faithfulness(answer, contexts):.2f}"
          f"   ← 每句话有多少 token 能在上下文里找到")
    print(f"  context_precision {score_context_precision(contexts, K1):.2f}"
          f"   ← 相关片段占全部片段的比例")
    print(f"  context_recall    {score_context_recall(contexts, K1):.2f}"
          f"   ← 关键事实被覆盖的比例")

    print()
    print("  换成编造的答复：")
    fake = "认证失败请立刻联系厂商，他们会远程解锁账号。"
    print(f"  faithfulness      {score_faithfulness(fake, contexts):.2f}"
          f"   ← 上下文里没有「厂商」「远程解锁」，所以是 0")
    print()
    print("  >>> faithfulness 抓的是「答复有没有超出给定资料」，这是幻觉的第一道闸。")


def demo_scores(reports: dict[str, RagReport]) -> None:
    header("第 3 步：四个变体的分数")
    print(f"  {pad('配置', 16)}{pad('faithfulness', 15)}{pad('correctness', 13)}"
          f"{pad('precision', 12)}{pad('recall', 10)}诊断")
    print("  " + "-" * 88)

    for name, report in reports.items():
        mean = report.mean()
        diagnosis = diagnose(report)
        print(f"  {pad(name, 16)}{pad(f'{mean["faithfulness"]:.2f}', 15)}"
              f"{pad(f'{mean["answer_correctness"]:.2f}', 13)}"
              f"{pad(f'{mean["context_precision"]:.2f}', 12)}"
              f"{pad(f'{mean["context_recall"]:.2f}', 10)}{diagnosis.label}")

    print()
    print("  >>> 注意这四个变体的「总分」看起来差不多（都在 0.4–1.0），")
    print("      但**指标组合模式完全不同**——这才是能定位病根的信息。")


def demo_diagnosis(reports: dict[str, RagReport]) -> None:
    header("第 4 步：从分数反推病根")
    for name in ("noisy", "narrow", "hallucinating"):
        diagnosis = diagnose(reports[name])
        print(f"  【{name}】{diagnosis.label}")
        print(f"    证据: {'  '.join(diagnosis.evidence)}")
        print(f"    建议: {diagnosis.advice}")
        worst_metric = "context_precision" if diagnosis.root_cause == "retrieval-noisy" else (
            "context_recall" if diagnosis.root_cause == "retrieval-narrow" else "faithfulness"
        )
        question, score = reports[name].worst(worst_metric)
        print(f"    最差样本: {question}  ({worst_metric}={score:.2f})")
        print()

    print("  定位规则（组合判断，单看一项定不了位）：")
    rows = [
        ("precision 低 + recall 高", "检索太宽（噪声多）", "缩 top_k、加阈值、加 rerank"),
        ("recall 低 + precision 高", "检索太窄（漏检）", "加 top_k、改切分、查询改写"),
        ("两项都低", "检索整体失效", "先查索引与 embedding 是否对得上"),
        ("precision/recall 高 + faithfulness 低", "生成幻觉", "收紧提示词、加引用校验"),
        ("correctness 低", "答错/答非所问", "查意图路由与技能选择"),
    ]
    print(f"  {pad('模式', 38)}{pad('病根', 22)}对策")
    print("  " + "-" * 96)
    for pattern, cause, advice in rows:
        print(f"  {pad(pattern, 38)}{pad(cause, 22)}{advice}")


def demo_compare(reports: dict[str, RagReport]) -> None:
    header("第 5 步：一张表看「哪个改动把哪项推高了」")
    rows = compare(list(reports.values()))
    print(f"  {pad('配置', 16)}{pad('faith', 8)}{pad('correct', 10)}"
          f"{pad('precision', 11)}{pad('recall', 8)}病根")
    print("  " + "-" * 70)
    for row in rows:
        print(f"  {pad(row['name'], 16)}{pad(f'{row["faithfulness"]:.2f}', 8)}"
              f"{pad(f'{row["answer_correctness"]:.2f}', 10)}"
              f"{pad(f'{row["context_precision"]:.2f}', 11)}"
              f"{pad(f'{row["context_recall"]:.2f}', 8)}{row['root_cause']}")
    print()
    print("  >>> 这张表就是 RAG 调优的仪表盘：改一个参数，看哪一列动了。")
    print("      **只有一列变而其它不动**，才说明你改对了地方。")


def demo_checklist() -> None:
    header("第 6 步：RAG 评测的自检清单")
    checks = [
        ("每条样本都有 ground_truth 吗", "没有参考答案就只能算 faithfulness 与 precision"),
        ("key_facts 标了吗", "recall 依赖它；没标就退化成用参考答案切句"),
        ("检索片段的顺序保留了吗", "排序质量影响真实体验，别只存集合"),
        ("指标能定位到具体样本吗", "只有平均分 = 知道病了但不知道哪疼"),
        ("知道自己的指标和 RAGAS 的差异吗", "别把代理指标的数字当 RAGAS 用"),
        ("改参数后是「一列动」还是「全动」", "全动说明改动影响面太大，不可归因"),
    ]
    for index, (question, why) in enumerate(checks, 1):
        print(f"  {index}. {question}")
        print(f"     └─ {why}")


def main() -> int:
    demo_ragas_reality()
    demo_single_metrics()

    reports = {name: evaluate(name, samples) for name, samples in build_variants().items()}
    demo_scores(reports)
    demo_diagnosis(reports)
    demo_compare(reports)
    demo_checklist()

    header("小结")
    print("  1. RAGAS 需要 LLM 才能算分；离线场景只能做确定性代理指标，且**不可与公开分数对比**。")
    print("  2. 四项指标的价值不在「总分」，而在**组合模式**：精度低+召回高=太宽，反之=太窄。")
    print("  3. 检索好而 faithfulness 低 = 生成幻觉；这一条能省掉大量「瞎调 top_k」的时间。")
    print("  4. 报告要能指到**最差样本**，否则你知道病了却不知道哪疼。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
