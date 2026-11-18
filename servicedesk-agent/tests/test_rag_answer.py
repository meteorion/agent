# file: tests/test_rag_answer.py
"""1.8 的断言：拼上下文、解析、引用校验、证据不足、编造引用。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.mock_llm_server import start_mock_server
from servicedesk.config import Settings
from servicedesk.llm.client import LLMClient
from servicedesk.rag.answer import (
    NO_EVIDENCE_ANSWER,
    REJECTED_ANSWER,
    AnswerParseError,
    build_context,
    build_messages,
    answer_question,
    parse_answer,
    render_result,
    to_json,
    validate_citations,
)
from servicedesk.rag.embedder import HashEmbedder
from servicedesk.rag.splitter import split_markdown
from servicedesk.rag.store import Hit, QdrantStore, index_chunks

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"


def make_hit(chunk_id: str = "doc#000", text: str = "示例正文") -> Hit:
    return Hit(
        chunk_id=chunk_id,
        doc_id="doc",
        order=0,
        heading_path=["手册", "1. 小节"],
        text=text,
        score=0.5,
    )


def load_chunks():
    chunks = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        chunks.extend(
            split_markdown(
                path.read_text(encoding="utf-8"), path.stem, max_chars=400, overlap_chars=80
            )
        )
    return chunks


@pytest.fixture(scope="module")
def mock_base_url():
    server, url = start_mock_server()
    yield url
    server.shutdown()


def make_settings(base_url: str) -> Settings:
    return Settings(
        _env_file=None,
        llm_base_url=base_url,
        llm_model="mock-model",
        llm_api_key="mock-key",
    )


# ------------------------------- 拼上下文 -------------------------------


def test_build_context_numbers_and_maps() -> None:
    hits = [make_hit("a#0", "第一段"), make_hit("b#1", "第二段")]
    context, index_map = build_context(hits)

    assert "[C1]" in context and "[C2]" in context
    assert index_map[1].chunk_id == "a#0"
    assert index_map[2].chunk_id == "b#1"
    assert "第一段" in context


def test_build_messages_has_system_and_question() -> None:
    messages = build_messages("怎么办", "[C1] doc=x § y\n正文")
    assert messages[0]["role"] == "system"
    assert "只能使用【资料】" in messages[0]["content"]
    assert "【问题】\n怎么办" in messages[1]["content"]


def test_empty_context_uses_placeholder() -> None:
    messages = build_messages("怎么办", "")
    assert "（无相关片段）" in messages[1]["content"]


# -------------------------------- 解析 --------------------------------


def test_parse_answer_ok() -> None:
    payload = parse_answer('{"answer": "答案[C1]", "citations": [{"index": 1, "quote": "原文"}]}')
    assert payload.answer == "答案[C1]"
    assert payload.citations[0].index == 1


def test_parse_answer_strips_code_fence() -> None:
    raw = '```json\n{"answer": "答案", "citations": []}\n```'
    assert parse_answer(raw).answer == "答案"


def test_parse_answer_rejects_garbage() -> None:
    with pytest.raises(AnswerParseError) as excinfo:
        parse_answer("我觉得应该重启一下")
    assert "JSON" in excinfo.value.repair_message


def test_parse_answer_rejects_bad_index() -> None:
    with pytest.raises(AnswerParseError):
        parse_answer('{"answer": "答案", "citations": [{"index": 0}]}')


# ------------------------------- 引用校验 -------------------------------


def test_validate_drops_unknown_index() -> None:
    payload = parse_answer('{"answer": "x", "citations": [{"index": 9}]}')
    cited, dropped = validate_citations(payload, {1: make_hit()})
    assert cited == []
    assert dropped == [9]


def test_validate_dedupes_same_index() -> None:
    payload = parse_answer(
        '{"answer": "x", "citations": [{"index": 2}, {"index": 2}, {"index": 1}]}'
    )
    cited, dropped = validate_citations(payload, {1: make_hit("a#0"), 2: make_hit("b#1")})
    assert [item.index for item in cited] == [2, 1]
    assert dropped == []


# ------------------------------- 端到端 -------------------------------


async def test_no_evidence_does_not_call_model(mock_base_url: str) -> None:
    """空库 → 直接返回"查不到"，不花一分钱。"""
    embedder = HashEmbedder()
    store = QdrantStore(dim=embedder.dim)
    store.recreate()          # 建了集合但不写入任何点

    async with LLMClient(make_settings(mock_base_url)) as client:
        result = await answer_question(client, store, embedder, "VPN 认证失败怎么办")

    assert result.no_evidence is True
    assert result.answer == NO_EVIDENCE_ANSWER
    assert result.cost_cny == 0.0
    assert client.stats.calls == 0, "证据不足时不该发起模型调用"


async def test_end_to_end_grounded_answer(mock_base_url: str) -> None:
    embedder = HashEmbedder()
    store = QdrantStore(dim=embedder.dim)
    store.recreate()
    await index_chunks(store, load_chunks(), embedder)

    async with LLMClient(make_settings(mock_base_url)) as client:
        result = await answer_question(client, store, embedder, "VPN 提示认证失败怎么办")

    assert result.no_evidence is False
    assert result.parse_ok is True
    assert result.grounded is True
    assert result.citations
    assert result.citations[0].chunk_id.startswith("vpn-guide#")
    assert result.citations[0].doc_id == "vpn-guide"
    assert result.cost_cny > 0


async def test_irrelevant_question_gets_no_evidence(mock_base_url: str) -> None:
    embedder = HashEmbedder()
    store = QdrantStore(dim=embedder.dim)
    store.recreate()
    await index_chunks(store, load_chunks(), embedder)

    async with LLMClient(make_settings(mock_base_url)) as client:
        result = await answer_question(client, store, embedder, "公司年会在哪里办")

    assert result.no_evidence is True
    assert result.evidence_score < 0.15


async def test_hallucinated_citation_rejects_whole_answer(mock_base_url: str) -> None:
    """模型引用不存在的 [C9] → 整段回答作废，原文留档。"""
    embedder = HashEmbedder()
    store = QdrantStore(dim=embedder.dim)
    store.recreate()
    await index_chunks(store, load_chunks(), embedder)

    async with LLMClient(make_settings(mock_base_url)) as client:
        result = await answer_question(
            client,
            store,
            embedder,
            "VPN 认证失败怎么办",
            extra_instruction="#hallucinate",
        )

    assert result.rejected is True
    assert result.answer == REJECTED_ANSWER
    assert result.dropped_citations == [9]
    assert "[C9]" in result.raw_answer, "原始回答必须保留供审计"
    assert "[C9]" not in result.answer, "对外呈现的回答里不能残留假引用"


# ------------------------------- 渲染 -------------------------------


async def test_render_and_json_include_citations(mock_base_url: str) -> None:
    embedder = HashEmbedder()
    store = QdrantStore(dim=embedder.dim)
    store.recreate()
    await index_chunks(store, load_chunks(), embedder)

    async with LLMClient(make_settings(mock_base_url)) as client:
        result = await answer_question(client, store, embedder, "VPN 提示认证失败怎么办")

    text = render_result(result)
    assert "引用：" in text
    assert "[C1]" in text

    payload = json.loads(to_json(result))
    assert payload["grounded"] is True
    assert payload["citations"][0]["chunk_id"]
    assert "evidence_score" in payload
