# file: src/servicedesk/eval/rag_eval.py
"""RAG 评测：四项指标 + 从分数反推病根。

⚠️ **重要说明（必须先读）**

`ragas 0.4.4` 能装、能 import，但**离线跑不了**。实测：

    >>> Faithfulness()
    TypeError: Faithfulness.__init__() missing 1 required positional argument: 'llm'

四项指标的构造函数都需要 `llm: InstructorBaseRagasLLM`（`AnswerRelevancy` 还要 embeddings）。
没有 API key 就没有分数——**RAGAS 的分数本质上是"模型判断的结论"**。

所以本模块实现**同名的确定性代理指标**（token 覆盖率，不调模型）：

    指标                 RAGAS 做法                    本模块做法
    ------------------  ----------------------------  ----------------------------
    faithfulness        用 LLM 把答复拆成 claim，逐条  按句切分，算句子 token 被
                        判断能否被 context 支持        context 覆盖的比例
    answer_correctness  对应 RAGAS 的 answer_relevancy 答复覆盖了多少"参考要点"的
                        （它用 LLM 反推问题再算相似度）token。**这是本节替换的指标**
    context_precision   用 LLM 判断每个 chunk 是否     key_fact 是否出现在 chunk 里，
                        与问题相关（排序加权）        算"相关片段占比"
    context_recall      用 LLM 抽取参考答案的 claim，  预标注的 key_facts 被
                        看 context 覆盖了多少         context 覆盖的比例

> **为什么把 answer_relevancy 换成 answer_correctness**：本节试过字符级覆盖率与哈希向量
> 余弦，前者把正确答案判成 0.14、后者对正确答案给出 0.000——都不可用。
> 与其硬凑一个误导性的数字，不如换成一个能算准的指标并**明确标注替换**。

**代理指标能做什么**：诊断病根（检索错 vs 生成错）、比较相对变化、进 CI 卡回归。
**代理指标不能做什么**：与论文/公开榜单的 RAGAS 分数对比；判断"语义等价但用词不同"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[a-zA-Z0-9]+")
_SENTENCE_RE = re.compile(r"[^。！？；\n]+")


def tokens(text: str) -> set[str]:
    """粗粒度分词：中文按字、英文按词。够做覆盖率，不追求语言学正确。"""
    if not text:
        return set()
    return set(_CJK_RE.findall(text)) | {word.lower() for word in _WORD_RE.findall(text)}


def coverage(need: set[str], have: set[str]) -> float:
    if not need:
        return 1.0
    return len(need & have) / len(need)


# ------------------------------- 样本与结果 -------------------------------


@dataclass
class RagSample:
    question: str
    ground_truth: str
    contexts: list[str]                 # 检索到的片段（有序，越靠前越相关）
    answer: str
    key_facts: list[str] = field(default_factory=list)   # 预标注的关键事实（用于 recall）

    def __post_init__(self) -> None:
        if not self.key_facts:
            # 没标注就拿参考答案切句，作为退化的 key_facts
            self.key_facts = [part.strip() for part in _SENTENCE_RE.findall(self.ground_truth) if part.strip()]


@dataclass
class RagScores:
    faithfulness: float
    answer_correctness: float
    context_precision: float
    context_recall: float

    def as_dict(self) -> dict[str, float]:
        return {
            "faithfulness": round(self.faithfulness, 3),
            "answer_correctness": round(self.answer_correctness, 3),
            "context_precision": round(self.context_precision, 3),
            "context_recall": round(self.context_recall, 3),
        }


# ------------------------------- 四项指标 -------------------------------


def score_faithfulness(answer: str, contexts: list[str], *, support_threshold: float = 0.6) -> float:
    """答复里的每句话，有多少能被 context 支持。

    "支持"的判据：这句话的 token 有 `support_threshold` 以上出现在 context 里。
    这是一个**保守**的判据：它会把"换了说法但意思对"的句子判成不支持。
    """
    sentences = [part.strip() for part in _SENTENCE_RE.findall(answer) if part.strip()]
    if not sentences:
        return 0.0
    context_tokens: set[str] = set()
    for context in contexts:
        context_tokens |= tokens(context)

    supported = sum(
        1 for sentence in sentences if coverage(tokens(sentence), context_tokens) >= support_threshold
    )
    return supported / len(sentences)


def score_answer_correctness(ground_truth: str, answer: str) -> float:
    """答复覆盖了多少"参考要点"。

    这是**替换指标**（见模块开头）：RAGAS 的 answer_relevancy 需要 LLM 反推问题，
    本节离线做不到；改用与参考答案的字面覆盖，衡量"答得对不对"。

    局限：用同义词作答（"重设" vs "重置"）会被低估。
    """
    return coverage(tokens(ground_truth), tokens(answer))


def score_context_precision(contexts: list[str], key_facts: Iterable[str]) -> float:
    """检索精度 = 相关片段 / 全部片段。

    相关判据：该片段覆盖了至少一个 key_fact 的 token（≥50%）。

    为什么不用排序加权 AP：实测"相关片段在前、噪声在后"时 AP 仍是 1.0，
    诊断不出"检索太宽"。而"相关片段占比"能直接把噪声暴露出来。
    代价：不再反映"排序好坏"——排序质量交给 1.8 的 rerank 指标去看。
    """
    facts = [tokens(fact) for fact in key_facts]
    if not facts or not contexts:
        return 0.0
    relevant = sum(
        1 for context in contexts
        if any(coverage(fact, tokens(context)) >= 0.5 for fact in facts)
    )
    return relevant / len(contexts)


def score_context_recall(contexts: list[str], key_facts: Iterable[str]) -> float:
    """关键事实被 context 覆盖的比例。"""
    facts = [tokens(fact) for fact in key_facts]
    if not facts:
        return 0.0
    context_tokens: set[str] = set()
    for context in contexts:
        context_tokens |= tokens(context)
    covered = sum(1 for fact in facts if coverage(fact, context_tokens) >= 0.5)
    return covered / len(facts)


def score_sample(sample: RagSample) -> RagScores:
    return RagScores(
        faithfulness=score_faithfulness(sample.answer, sample.contexts),
        answer_correctness=score_answer_correctness(sample.ground_truth, sample.answer),
        context_precision=score_context_precision(sample.contexts, sample.key_facts),
        context_recall=score_context_recall(sample.contexts, sample.key_facts),
    )


# ------------------------------- 报告与诊断 -------------------------------

# 诊断阈值（经验值，需按业务校准）
LOW = 0.6
GOOD = 0.7


@dataclass
class RagReport:
    name: str
    scores: list[RagScores] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)

    def mean(self) -> dict[str, float]:
        if not self.scores:
            return {key: 0.0 for key in RagScores(0, 0, 0, 0).as_dict()}
        return {
            key: round(sum(getattr(score, key) for score in self.scores) / len(self.scores), 3)
            for key in self.scores[0].as_dict()
        }

    def worst(self, metric: str) -> tuple[str, float]:
        """最差的那条样本——排障从这里开始。"""
        pairs = list(zip(self.questions, (getattr(score, metric) for score in self.scores)))
        return min(pairs, key=lambda item: item[1]) if pairs else ("", 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "samples": len(self.scores), **self.mean()}


def evaluate(name: str, samples: list[RagSample]) -> RagReport:
    return RagReport(
        name=name,
        scores=[score_sample(sample) for sample in samples],
        questions=[sample.question for sample in samples],
    )


# ------------------------------- 病根定位 -------------------------------


@dataclass
class Diagnosis:
    root_cause: str          # retrieval-narrow / retrieval-noisy / generation-hallucination / off-topic / healthy
    label: str               # 中文说法
    evidence: list[str]
    advice: str


def diagnose(report: RagReport) -> Diagnosis:
    """按四项指标的**组合模式**反推病根。

    单看某一项是定不了位的——同一个"分数低"，可能是检索的锅，也可能是生成的锅。
    """
    mean = report.mean()
    precision = mean["context_precision"]
    recall = mean["context_recall"]
    faithfulness = mean["faithfulness"]
    correctness = mean["answer_correctness"]

    evidence = [
        f"context_precision={precision:.2f}",
        f"context_recall={recall:.2f}",
        f"faithfulness={faithfulness:.2f}",
        f"answer_correctness={correctness:.2f}",
    ]

    # 检索太宽：噪声多（精度低），但该有的都在（召回高）
    if precision < LOW and recall >= GOOD:
        return Diagnosis(
            "retrieval-noisy", "检索太宽（召回多但噪声大）", evidence,
            "缩小 top_k、加相似度阈值、加 rerank；先看最差样本里哪些片段是纯噪声。",
        )

    # 检索太窄：该有的没找到（召回低），但找到的都相关（精度高）
    if recall < LOW and precision >= GOOD:
        return Diagnosis(
            "retrieval-narrow", "检索太窄（漏检）", evidence,
            "加大 top_k、改进 chunk 切分、加同义词/查询改写；先看漏了哪条 key_fact。",
        )

    # 检索两头都不行
    if precision < LOW and recall < LOW:
        return Diagnosis(
            "retrieval-broken", "检索整体失效", evidence,
            "先查索引与 embedding 是否对得上（维度、归一化、语言），再谈 top_k。",
        )

    # 检索够好，但答复不支持 → 生成幻觉
    if precision >= GOOD and recall >= GOOD and faithfulness < LOW:
        return Diagnosis(
            "generation-hallucination", "生成幻觉（上下文对，答复编）", evidence,
            "收紧提示词（只允许依据给定资料）、加引用校验（1.8 的证据门）、降低温度。",
        )

    # 答复与问题不搭/答错
    if correctness < LOW:
        return Diagnosis(
            "off-topic", "答非所问", evidence,
            "查意图识别与路由；可能是把问题分到了错的技能上。",
        )

    return Diagnosis("healthy", "健康", evidence, "保持当前配置，继续扩大评测集。")


def compare(reports: list[RagReport]) -> list[dict[str, Any]]:
    """把多份报告摆成一张表，便于看"哪个改动把哪项指标推高了"。"""
    rows = []
    for report in reports:
        diagnosis = diagnose(report)
        rows.append({**report.as_dict(), "root_cause": diagnosis.root_cause, "label": diagnosis.label})
    return rows
