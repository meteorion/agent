# file: scripts/rag_demo.py
"""1.7 的场景：建库 → 检索 → 过滤 → 阈值 → rerank。

默认用本地 HashEmbedder（零依赖、零成本）；
加 --remote 则改走 mock 服务的 /embeddings 接口，验证"远程批量 + 并发"那条链路。

用法：
    uv run python -m scripts.rag_demo
    uv run python -m scripts.rag_demo --remote
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from scripts.mock_llm_server import start_mock_server
from servicedesk.config import Settings
from servicedesk.logging_setup import configure_logging
from servicedesk.rag.embedder import Embedder, HashEmbedder, RemoteEmbedder
from servicedesk.rag.splitter import Chunk, split_markdown
from servicedesk.rag.store import Hit, QdrantStore, index_chunks, rerank, retrieve

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"

QUERY = "VPN 提示认证失败怎么办"
ANSWER_HINT = "重置密码"
IRRELEVANT = "食堂几点开饭"
FILTERED_QUERY = "垃圾邮件误判"

# 两段 rerank 演示：一段"挤掉泛化块"，一段"把错的 top1 换对"
RERANK_CASES: list[tuple[str, str]] = [
    (QUERY, ANSWER_HINT),
    ("邮件附件上限", "25 MB"),
]


def header(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def short(text: str, limit: int = 40) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def show_hits(hits: list[Hit], *, with_rerank: bool = False) -> None:
    if not hits:
        print("    （空）")
        return
    for rank, hit in enumerate(hits, start=1):
        section = hit.heading_path[-1] if hit.heading_path else "（无标题）"
        tail = (
            f"  rerank={hit.rerank_score:.3f}"
            if with_rerank and hit.rerank_score is not None
            else ""
        )
        print(f"    {rank}. vec={hit.score:.3f}{tail}  {hit.chunk_id:<16} § {section}")
        print(f"       {short(hit.text)}")


def load_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        chunks.extend(split_markdown(text, path.stem, max_chars=400, overlap_chars=80))
    return chunks


def compare(label: str, query: str, answer_hint: str, hits: list[Hit]) -> None:
    before = hits[:3]
    after = rerank(query, hits, top_k=3)
    print(f"\n  【{label}】{query}")
    print("    rerank 前 top3：")
    show_hits(before)
    print("    rerank 后 top3：")
    show_hits(after, with_rerank=True)

    before_ids = [hit.chunk_id for hit in before]
    after_ids = [hit.chunk_id for hit in after]
    dropped = [cid for cid in before_ids if cid not in after_ids]
    entered = [cid for cid in after_ids if cid not in before_ids]
    print(f"    top1：{before_ids[0]} → {after_ids[0]}"
          f"{'  ✅ 换对了' if before_ids[0] != after_ids[0] else '  （未变）'}")
    print(f"    进入：{entered or '无'}    挤出：{dropped or '无'}")
    contains_answer = any(answer_hint in hit.text for hit in after)
    print(f"    top3 是否含实际答案（{answer_hint}）：{'是' if contains_answer else '否'}")


async def run(embedder: Embedder, label: str) -> None:
    header(f"第 1 步：建库（embedder = {label}，dim={embedder.dim}）")
    chunks = load_chunks()
    store = QdrantStore(dim=embedder.dim)
    store.recreate()

    written = await index_chunks(store, chunks, embedder)
    print(f"  切分得到 {len(chunks)} 块，写入 {written} 点，库内 count={store.count()}")
    print(f"  平均每块 {sum(c.char_len for c in chunks) // len(chunks)} 字")

    header(f"第 2 步：检索「{QUERY}」")
    hits = await retrieve(store, embedder, QUERY, top_k=5)
    show_hits(hits)
    print(f"\n  最高分 = {hits[0].score:.3f}" if hits else "  无命中")

    header("第 3 步：阈值——不相关的问题应该「检索为空」")
    irrelevant = await retrieve(store, embedder, IRRELEVANT, top_k=5)
    show_hits(irrelevant)
    if hits and irrelevant:
        print(
            f"\n  相关查询最高分 {hits[0].score:.3f}  vs  "
            f"无关查询最高分 {irrelevant[0].score:.3f}"
        )
    print("\n  取阈值 0.30 再查一次无关问题 →")
    show_hits(await retrieve(store, embedder, IRRELEVANT, top_k=5, min_score=0.30))
    print("  >>> 检索为空是**正常结果**，上层必须能处理「没有证据」这种情形。")

    header("第 4 步：按文档过滤（多租户 / 权限隔离的基础）")
    print(f"  查询「{FILTERED_QUERY}」，只在 email-faq 里找：")
    show_hits(await retrieve(store, embedder, FILTERED_QUERY, top_k=3, doc_ids=["email-faq"]))
    print(f"\n  同一查询，只在 vpn-guide 里找：")
    show_hits(await retrieve(store, embedder, FILTERED_QUERY, top_k=3, doc_ids=["vpn-guide"]))
    print("  >>> 注意第二组虽然也返回了结果，但明显更弱——过滤只能限定范围，不能制造相关。")

    header("第 5 步：rerank——只改顺序，不改召回")
    print("  rerank 不重新检索，只是把已召回的候选重新排序。")
    for query, answer in RERANK_CASES:
        hits_for_query = await retrieve(store, embedder, query, top_k=5)
        compare("案例", query, answer, hits_for_query)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--remote", action="store_true", help="用远程 /embeddings（打到本地 mock）"
    )
    args = parser.parse_args()

    configure_logging(level="WARNING")

    if not args.remote:
        await run(HashEmbedder(), "HashEmbedder（本地）")
        return 0

    server, base_url = start_mock_server()
    settings = Settings(_env_file=None, llm_base_url=base_url, llm_api_key="mock-key")
    try:
        embedder = RemoteEmbedder(
            settings=settings, model="mock-embed", batch_size=8, concurrency=3
        )
        await run(embedder, "RemoteEmbedder（批量 8 / 并发 3）")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
