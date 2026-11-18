# file: tests/test_rag_store.py
"""1.7 的断言：向量确定性、批量顺序、过滤、阈值、rerank 真的能换掉 top1。"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from scripts.mock_llm_server import embed_text, start_mock_server
from servicedesk.config import Settings
from servicedesk.rag.embedder import EMBED_DIM, HashEmbedder, RemoteEmbedder, hash_vector
from servicedesk.rag.splitter import split_markdown
from servicedesk.rag.store import (
    QdrantStore,
    index_chunks,
    point_id,
    query_terms,
    rerank,
    retrieve,
)

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"


def load_chunks():
    chunks = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        chunks.extend(
            split_markdown(
                path.read_text(encoding="utf-8"), path.stem, max_chars=400, overlap_chars=80
            )
        )
    return chunks


# ------------------------------- 向量化 -------------------------------


def test_hash_vector_is_deterministic_and_normalized() -> None:
    first = hash_vector("VPN 提示认证失败")
    second = hash_vector("VPN 提示认证失败")
    assert first == second
    assert len(first) == EMBED_DIM
    norm = math.sqrt(sum(value * value for value in first))
    assert norm == pytest.approx(1.0)


def test_hash_vector_is_not_process_random() -> None:
    """必须用 crc32 而不是内置 hash()，否则换进程向量就变了。"""
    assert hash_vector("测试文本") == hash_vector("测试文本")
    # 与 mock 服务端算出来的必须一致，说明两边算法没跑偏
    assert hash_vector("VPN 认证失败") == embed_text("VPN 认证失败")


def test_similar_text_is_closer_than_unrelated() -> None:
    base = hash_vector("VPN 认证失败")
    near = hash_vector("VPN 认证失败怎么办")
    far = hash_vector("邮件附件上限")

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))

    assert cosine(base, near) > cosine(base, far)


# ------------------------------ 远程 embedding ------------------------------


@pytest.fixture(scope="module")
def mock_base_url():
    server, url = start_mock_server()
    yield url
    server.shutdown()


async def test_remote_embedder_preserves_order_across_batches(mock_base_url: str) -> None:
    """batch_size=3 + 7 条文本 → 3 个批次，但结果顺序必须与输入一致。"""
    texts = [f"文本{i}" for i in range(7)]
    embedder = RemoteEmbedder(
        settings=Settings(_env_file=None, llm_base_url=mock_base_url, llm_api_key="k"),
        model="mock-embed",
        batch_size=3,
        concurrency=2,
    )
    vectors = await embedder.embed(texts)

    assert len(vectors) == 7
    for text, vector in zip(texts, vectors):
        assert vector == embed_text(text), f"顺序错位：{text}"


async def test_remote_embedder_respects_dim(mock_base_url: str) -> None:
    embedder = RemoteEmbedder(
        settings=Settings(_env_file=None, llm_base_url=mock_base_url, llm_api_key="k"),
        model="mock-embed",
        dim=64,
    )
    (vector,) = await embedder.embed(["任意文本"])
    assert len(vector) == 64


async def test_remote_embedder_empty_input() -> None:
    embedder = RemoteEmbedder(dim=8)
    assert await embedder.embed([]) == []


async def test_remote_embedder_needs_human_on_401(mock_base_url: str) -> None:
    from servicedesk.errors import NeedsHumanError

    embedder = RemoteEmbedder(
        settings=Settings(_env_file=None, llm_base_url=mock_base_url, llm_api_key=None),
        model="mock-embed",
    )
    with pytest.raises(NeedsHumanError):
        await embedder.embed(["文本"])


# ---------------------------------- 存储 ----------------------------------


def test_point_id_is_stable_and_unique() -> None:
    assert point_id("vpn-guide#007") == point_id("vpn-guide#007")
    assert point_id("vpn-guide#007") != point_id("vpn-guide#008")


async def test_index_and_search_finds_expected_chunk() -> None:
    chunks = load_chunks()
    embedder = HashEmbedder()
    store = QdrantStore(dim=embedder.dim)
    store.recreate()

    written = await index_chunks(store, chunks, embedder)
    assert written == len(chunks)
    assert store.count() == len(chunks)

    hits = await retrieve(store, embedder, "VPN 提示认证失败怎么办", top_k=3)
    assert hits
    assert hits[0].heading_path[-1] == "4.1 提示「认证失败」"


async def test_filter_restricts_documents() -> None:
    chunks = load_chunks()
    embedder = HashEmbedder()
    store = QdrantStore(dim=embedder.dim)
    store.recreate()
    await index_chunks(store, chunks, embedder)

    hits = await retrieve(store, embedder, "垃圾邮件误判", top_k=3, doc_ids=["email-faq"])
    assert hits
    assert all(hit.doc_id == "email-faq" for hit in hits)

    other = await retrieve(store, embedder, "垃圾邮件误判", top_k=3, doc_ids=["vpn-guide"])
    assert all(hit.doc_id == "vpn-guide" for hit in other)


async def test_min_score_can_return_empty() -> None:
    """无关问题 + 阈值 → 空列表。这是正常结果，不是错误。"""
    chunks = load_chunks()
    embedder = HashEmbedder()
    store = QdrantStore(dim=embedder.dim)
    store.recreate()
    await index_chunks(store, chunks, embedder)

    raw = await retrieve(store, embedder, "食堂几点开饭", top_k=3)
    assert raw, "不过阈值时应该能拿到候选（哪怕不相关）"

    assert await retrieve(store, embedder, "食堂几点开饭", top_k=3, min_score=0.9) == []


def test_upsert_rejects_length_mismatch() -> None:
    store = QdrantStore(dim=4)
    store.recreate()
    with pytest.raises(ValueError):
        store.upsert(load_chunks()[:2], [[0.0] * 4])


# --------------------------------- rerank ---------------------------------


def test_query_terms_splits_chinese() -> None:
    terms = query_terms("VPN 提示认证失败")
    assert "VPN" in terms
    assert "认证" in terms


def test_rerank_does_not_invent_new_chunks() -> None:
    """rerank 只能在已有候选里排序，不能凭空空降。"""
    from servicedesk.rag.store import Hit

    hits = [
        Hit("a#0", "a", 0, ["标题"], "完全无关的内容", 0.9),
        Hit("b#0", "b", 0, ["标题"], "认证失败的处理办法", 0.1),
    ]
    ordered = rerank("认证失败", hits, top_k=2)
    assert {hit.chunk_id for hit in ordered} == {"a#0", "b#0"}


async def test_rerank_can_fix_a_wrong_top1() -> None:
    """实测：'邮件附件上限' 的向量 top1 是错的，rerank 之后才对。"""
    chunks = load_chunks()
    embedder = HashEmbedder()
    store = QdrantStore(dim=embedder.dim)
    store.recreate()
    await index_chunks(store, chunks, embedder)

    hits = store.search(hash_vector("邮件附件上限"), top_k=5)

    before = hits[0]
    after = rerank("邮件附件上限", hits, top_k=3)[0]

    assert "25 MB" not in before.text, "rerank 前 top1 应该不含答案（否则这个用例没意义）"
    assert "25 MB" in after.text
    assert after.rerank_score is not None
