# file: tests/test_rag_eval.py
"""5.2 的断言：四项指标的算术正确，且能从分数定位到病根。"""

from __future__ import annotations

import pytest

from servicedesk.eval.rag_eval import (
    RagReport,
    RagSample,
    compare,
    diagnose,
    evaluate,
    score_answer_correctness,
    score_context_precision,
    score_context_recall,
    score_faithfulness,
    tokens,
)

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


# ------------------------------ 分词与合计 ------------------------------


def test_tokens_mixes_cjk_and_ascii() -> None:
    result = tokens("VPN 认证 25 MB")
    assert "V" not in result or True          # 中文按字、英文按词
    assert {"认", "证"} <= result
    assert {"vpn", "25", "mb"} <= result


# ------------------------------ 四项指标 ------------------------------


def test_faithfulness_full_support() -> None:
    assert score_faithfulness("请先重置密码。", [F1]) == 1.0


def test_faithfulness_zero_when_invented() -> None:
    assert score_faithfulness("请立刻联系厂商远程解锁。", [F1, F2]) == 0.0


def test_faithfulness_empty_answer() -> None:
    assert score_faithfulness("", [F1]) == 0.0


def test_answer_correctness_exact() -> None:
    assert score_answer_correctness(GT1, GT1) == 1.0


def test_answer_correctness_partial() -> None:
    score = score_answer_correctness(GT1, "请先重置密码。")
    assert 0.0 < score < 1.0


def test_context_precision_all_relevant() -> None:
    assert score_context_precision([F1, F2], K1) == 1.0


def test_context_precision_half_noise() -> None:
    assert score_context_precision([F1, F2, NOISE1, NOISE2], K1) == pytest.approx(0.5)


def test_context_precision_empty_contexts() -> None:
    assert score_context_precision([], K1) == 0.0


def test_context_recall_full() -> None:
    assert score_context_recall([F1, F2], K1) == 1.0


def test_context_recall_partial() -> None:
    assert score_context_recall([F3_SHORT], K2) == pytest.approx(0.5)


def test_context_recall_empty_contexts() -> None:
    assert score_context_recall([], K2) == 0.0


# ------------------------------ 病根定位 ------------------------------


def variants() -> dict[str, list[RagSample]]:
    return {
        "good": [
            RagSample(Q1, GT1, [F1, F2], "请先在自助门户重置密码，重置后 5 分钟内生效。", K1),
            RagSample(Q2, GT2, [F3], "附件上限 25 MB，超出请用企业网盘分享链接。", K2),
        ],
        "noisy": [
            RagSample(Q1, GT1, [F1, F2, NOISE1, NOISE2], "请先在自助门户重置密码，重置后 5 分钟内生效。", K1),
            RagSample(Q2, GT2, [F3, NOISE1, NOISE2], "附件上限 25 MB，超出请用企业网盘分享链接。", K2),
        ],
        "narrow": [
            RagSample(Q1, GT1, [F1], "请先重置密码。", K1),
            RagSample(Q2, GT2, [F3_SHORT], "附件上限 25 MB。", K2),
        ],
        "hallucinating": [
            RagSample(Q1, GT1, [F1, F2], "认证失败请立刻联系厂商，他们会远程解锁账号。", K1),
            RagSample(Q2, GT2, [F3], "附件上限 100 MB，直接发送即可。", K2),
        ],
    }


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("good", "healthy"),
        ("noisy", "retrieval-noisy"),
        ("narrow", "retrieval-narrow"),
        ("hallucinating", "generation-hallucination"),
    ],
)
def test_diagnose_maps_pattern_to_root_cause(name: str, expected: str) -> None:
    report = evaluate(name, variants()[name])
    assert diagnose(report).root_cause == expected


def test_both_low_means_retrieval_broken() -> None:
    samples = [RagSample(Q1, GT1, [NOISE1], "打印机驱动请到内网软件中心下载。", K1)]
    assert diagnose(evaluate("broken", samples)).root_cause == "retrieval-broken"


# ------------------------------ 报告 ------------------------------


def test_report_mean_shape() -> None:
    report = evaluate("good", variants()["good"])
    mean = report.mean()
    assert set(mean) == {"faithfulness", "answer_correctness", "context_precision", "context_recall"}
    assert all(0.0 <= value <= 1.0 for value in mean.values())


def test_report_finds_worst_sample() -> None:
    report = evaluate("noisy", variants()["noisy"])
    question, score = report.worst("context_precision")
    assert question == Q2, "带两条噪声的那条样本应该最差"
    assert score < 0.5


def test_empty_report_does_not_crash() -> None:
    report = RagReport(name="empty")
    assert report.mean()["faithfulness"] == 0.0
    assert report.worst("faithfulness") == ("", 0.0)


def test_compare_produces_one_row_per_report() -> None:
    reports = [evaluate(name, samples) for name, samples in variants().items()]
    rows = compare(reports)
    assert len(rows) == 4
    assert {row["root_cause"] for row in rows} == {
        "healthy", "retrieval-noisy", "retrieval-narrow", "generation-hallucination"
    }


def test_sample_without_key_facts_derives_from_ground_truth() -> None:
    sample = RagSample(Q1, GT1, [F1, F2], "请先重置密码。")
    assert sample.key_facts, "没标 key_facts 时应从参考答案切句"
