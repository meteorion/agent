# file: src/servicedesk/rag/answer.py
"""把检索结果拼成 prompt，拿到一个「带引用、可追溯」的回答。

三条铁律（每条都对应一类生产事故）：
1. **只依据资料回答** —— 没有依据就明说，不要"根据常识"
2. **每条结论带 [Cn] 引用** —— 没有引用的回答无法审计
3. **引用必须真实存在** —— 模型会编造 [C9]，代码必须校验

最后一条是本节的重点：**prompt 只能"请求"模型遵守规则，不能"保证"它遵守。**
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Sequence

from pydantic import BaseModel, Field, ValidationError

from servicedesk.llm.client import LLMClient
from servicedesk.logging_setup import get_logger
from servicedesk.models.ticket import extract_json_object, strip_code_fence
from servicedesk.rag.embedder import Embedder
from servicedesk.rag.store import Hit, QdrantStore, rerank, retrieve

log = get_logger("rag.answer")

NO_EVIDENCE_ANSWER = "知识库里没有找到与这个问题相关的资料，建议提交 IT 工单或转人工。"
NO_EVIDENCE_PLACEHOLDER = "（无相关片段）"
# 模型编造引用时，整段回答作废——比"删掉引用、留下结论"安全得多
REJECTED_ANSWER = "资料中未能确认该问题的答案。为避免误导，已拒绝本次回答，请转人工复核。"

SYSTEM_PROMPT = """你是企业 IT 服务台的助理，负责依据内部手册回答员工问题。

必须遵守的规则：
1. 只能使用【资料】里出现的内容回答，不得使用常识或自行推测。
2. 每条结论后面用 [C1]、[C2] 这样的编号标注来源，编号必须出现在【资料】里。
3. 如果【资料】里没有能回答该问题的内容，answer 就写"资料中没有相关信息"，citations 留空。
4. 只输出一个 JSON 对象，格式如下，不要输出任何解释文字：
   {"answer": "...", "citations": [{"index": 1, "quote": "支撑这句话的原文片段"}]}
"""


# ------------------------------- 数据模型 -------------------------------


class CitationRef(BaseModel):
    """模型给出的引用：只要给编号和原文片段，chunk_id 由我们反查。"""

    index: int = Field(ge=1, description="对应【资料】里的 [Cn] 编号")
    quote: str = Field(default="", max_length=200, description="支撑该结论的原文片段")


class AnswerPayload(BaseModel):
    """模型输出的完整结构。"""

    answer: str = Field(min_length=1, max_length=2000)
    citations: list[CitationRef] = Field(default_factory=list)


@dataclass(frozen=True)
class CitedChunk:
    """校验通过后的引用：已经绑定到真实的检索结果。"""

    index: int
    chunk_id: str
    doc_id: str
    heading_path: list[str]
    quote: str

    @property
    def citation(self) -> str:
        path = " > ".join(self.heading_path) or "（无标题）"
        return f"[C{self.index}] {self.doc_id} § {path}"


@dataclass
class RagResult:
    question: str
    answer: str
    citations: list[CitedChunk] = field(default_factory=list)
    hit_ids: list[str] = field(default_factory=list)
    grounded: bool = False           # 有答案且有有效引用
    no_evidence: bool = False        # 证据不足，未调用模型
    parse_ok: bool = True            # 模型输出是否是合法 JSON
    dropped_citations: list[int] = field(default_factory=list)
    evidence_score: float = 0.0      # rerank 分（最高那条）
    cost_cny: float = 0.0
    rejected: bool = False           # 模型编造引用 → 整段回答作废
    raw_answer: str = ""             # 作废前的原始回答，留作审计


class AnswerParseError(ValueError):
    """模型输出不是合法的 AnswerPayload。"""

    def __init__(self, raw: str, detail: str) -> None:
        super().__init__(detail)
        self.raw = raw
        self.detail = detail

    @property
    def repair_message(self) -> str:
        return (
            f"你上一次的输出无法解析为规定结构：{self.detail}。\n"
            "请只输出一个 JSON 对象，形如：\n"
            '{"answer": "...", "citations": [{"index": 1, "quote": "..."}]}\n'
            "不要输出 markdown 代码块，也不要输出任何解释。"
        )


# ------------------------------- 拼 prompt -------------------------------


def build_context(hits: Sequence[Hit]) -> tuple[str, dict[int, Hit]]:
    """把命中拼成【资料】块，并返回 编号 → Hit 的映射（用于校验引用）。"""
    index_map: dict[int, Hit] = {}
    blocks: list[str] = []

    for position, hit in enumerate(hits, start=1):
        index_map[position] = hit
        path = " > ".join(hit.heading_path) or "（无标题）"
        blocks.append(f"[C{position}] doc={hit.doc_id} § {path}\n{hit.text.strip()}")

    return "\n\n".join(blocks), index_map


def build_messages(
    question: str, context: str, *, extra_instruction: str = ""
) -> list[dict[str, str]]:
    suffix = f"\n\n【附加要求】\n{extra_instruction}" if extra_instruction else ""
    user = f"【资料】\n{context or NO_EVIDENCE_PLACEHOLDER}\n\n【问题】\n{question}{suffix}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ------------------------------- 解析与校验 -------------------------------


def parse_answer(raw: str) -> AnswerPayload:
    """解析模型输出。失败抛 AnswerParseError（带可回喂的修复提示）。"""
    cleaned = strip_code_fence(raw)
    candidate = extract_json_object(cleaned)
    if candidate is None:
        raise AnswerParseError(raw, "输出里找不到 JSON 对象")

    try:
        return AnswerPayload.model_validate_json(candidate)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc") or ()) or "整体"
        raise AnswerParseError(raw, f"字段 {location}：{first.get('msg')}") from exc


def validate_citations(
    payload: AnswerPayload, index_map: dict[int, Hit]
) -> tuple[list[CitedChunk], list[int]]:
    """把模型给的编号映射回真实块；不存在的编号被丢弃并记录。"""
    cited: list[CitedChunk] = []
    dropped: list[int] = []
    seen: set[int] = set()

    for ref in payload.citations:
        if ref.index not in index_map:
            dropped.append(ref.index)
            continue
        if ref.index in seen:
            continue
        seen.add(ref.index)
        hit = index_map[ref.index]
        cited.append(
            CitedChunk(
                index=ref.index,
                chunk_id=hit.chunk_id,
                doc_id=hit.doc_id,
                heading_path=list(hit.heading_path),
                quote=ref.quote,
            )
        )

    if dropped:
        log.warning(
            "citations_dropped",
            dropped=dropped,
            available=sorted(index_map),
            reason="模型引用了不存在的资料编号",
        )
    return cited, dropped


# ------------------------------- 主流程 -------------------------------


async def answer_question(
    client: LLMClient,
    store: QdrantStore,
    embedder: Embedder,
    question: str,
    *,
    top_k: int = 5,
    rerank_min_score: float = 0.15,
    min_score: float | None = None,
    extra_instruction: str = "",
) -> RagResult:
    """检索 → rerank → 证据判定 → 拼 prompt → 调用 → 解析 → 校验引用。

    证据判定用 **rerank 分**而不是向量分：向量分只反映"字面像"，
    rerank 分才反映"问题里的词在这段资料里出现了多少"。
    实测本语料上，可答问题的 rerank 分 ≥0.22，不可答的 ≤0.10，
    用 0.15 就能干净切开；而向量分两者都在 0.17–0.43 之间，切不开。
    """
    hits = await retrieve(
        store, embedder, question, top_k=max(top_k * 2, 10), min_score=min_score
    )
    if not hits:
        log.info("answer_no_evidence", question=question, reason="向量检索为空")
        return RagResult(
            question=question,
            answer=NO_EVIDENCE_ANSWER,
            no_evidence=True,
            grounded=False,
        )

    ranked = rerank(question, hits, top_k=top_k)
    evidence = ranked[0].rerank_score or 0.0

    # 证据不足：**不调用模型**。既省钱，也彻底消除"没有证据却硬答"的可能。
    if evidence < rerank_min_score:
        log.info(
            "answer_no_evidence",
            question=question,
            reason="rerank 分低于阈值",
            evidence_score=round(evidence, 4),
            threshold=rerank_min_score,
        )
        return RagResult(
            question=question,
            answer=NO_EVIDENCE_ANSWER,
            hit_ids=[hit.chunk_id for hit in ranked],
            no_evidence=True,
            grounded=False,
            evidence_score=evidence,
        )

    context, index_map = build_context(ranked)
    messages = build_messages(question, context, extra_instruction=extra_instruction)
    call = await client.chat(messages)

    try:
        payload = parse_answer(call.text)
    except AnswerParseError as exc:
        # 解析失败不等于服务不可用：把原文降级返回，并标记未接地
        log.warning("answer_parse_failed", detail=exc.detail, raw=exc.raw[:200])
        return RagResult(
            question=question,
            answer=call.text.strip(),
            hit_ids=[hit.chunk_id for hit in ranked],
            grounded=False,
            parse_ok=False,
            evidence_score=evidence,
            cost_cny=call.cost_cny,
        )

    citations, dropped = validate_citations(payload, index_map)
    grounded = bool(citations)

    # 编造引用（引用了不存在的编号）→ 整段回答不可信，直接作废。
    # 只删引用、留下结论是危险的：用户仍会读到一个没有出处的断言。
    rejected = bool(dropped) and not grounded
    if rejected:
        log.warning(
            "answer_rejected",
            question=question,
            dropped=dropped,
            raw_answer_preview=payload.answer[:120],
            reason="模型引用了不存在的资料编号",
        )
        return RagResult(
            question=question,
            answer=REJECTED_ANSWER,
            hit_ids=[hit.chunk_id for hit in ranked],
            grounded=False,
            dropped_citations=dropped,
            evidence_score=evidence,
            cost_cny=call.cost_cny,
            rejected=True,
            raw_answer=payload.answer,
        )

    if payload.answer and not grounded:
        log.warning(
            "answer_ungrounded",
            question=question,
            answer_preview=payload.answer[:120],
            reason="没有给出任何引用",
        )

    return RagResult(
        question=question,
        answer=payload.answer,
        citations=citations,
        hit_ids=[hit.chunk_id for hit in ranked],
        grounded=grounded,
        dropped_citations=dropped,
        evidence_score=evidence,
        cost_cny=call.cost_cny,
    )


def render_result(result: RagResult) -> str:
    """把结果渲染成给人看的文本（含引用清单与告警）。"""
    lines = [f"问题：{result.question}", "", f"回答：{result.answer}"]

    if result.citations:
        lines.append("")
        lines.append("引用：")
        for cited in result.citations:
            lines.append(f"  {cited.citation}")
            if cited.quote:
                lines.append(f"    「{cited.quote}」")
    else:
        lines.append("")
        lines.append("引用：（无）")

    if result.dropped_citations:
        lines.append("")
        lines.append(f"⚠️ 已丢弃无效引用编号：{result.dropped_citations}")
    if result.rejected:
        lines.append(f"⚠️ 原始回答已作废（保留供审计）：{result.raw_answer[:120]}")
    if result.no_evidence:
        lines.append(
            f"⚠️ 证据不足（rerank 分 {result.evidence_score:.3f}），未调用模型"
        )
    elif not result.parse_ok:
        lines.append("⚠️ 模型输出不是合法 JSON，已降级为纯文本")
    elif result.rejected:
        lines.append("⚠️ 回答已作废（模型编造了不存在的引用）")
    elif not result.grounded:
        lines.append("⚠️ 回答未接地（没有有效引用），请人工复核")

    return "\n".join(lines)


def to_json(result: RagResult) -> str:
    return json.dumps(
        {
            "question": result.question,
            "answer": result.answer,
            "citations": [
                {
                    "index": cited.index,
                    "chunk_id": cited.chunk_id,
                    "doc_id": cited.doc_id,
                    "heading_path": cited.heading_path,
                    "quote": cited.quote,
                }
                for cited in result.citations
            ],
            "grounded": result.grounded,
            "no_evidence": result.no_evidence,
            "parse_ok": result.parse_ok,
            "dropped_citations": result.dropped_citations,
            "rejected": result.rejected,
            "raw_answer": result.raw_answer,
            "evidence_score": round(result.evidence_score, 4),
            "cost_cny": round(result.cost_cny, 6),
        },
        ensure_ascii=False,
        indent=2,
    )
