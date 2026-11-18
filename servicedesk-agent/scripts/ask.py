# file: scripts/ask.py
"""1.8 的成果物：M1 验收用的端到端小工具。

一条命令走完：切分 → 向量化 → 建库 → 检索 → 拼 prompt → 调用模型 → 校验引用。

用法：
    uv run python -m scripts.ask "VPN 提示认证失败怎么办"
    uv run python -m scripts.ask --demo           # 跑四个预设场景
    uv run python -m scripts.ask --json "问题"     # 机器可读输出

默认打到本地 mock（无需 API Key）。要接真实服务，把 .env 里的
LLM_BASE_URL / LLM_MODEL / LLM_API_KEY 填好，并把本文件的 make_settings 换成默认值。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from scripts.mock_llm_server import start_mock_server
from servicedesk.config import Settings
from servicedesk.llm.client import LLMClient
from servicedesk.logging_setup import configure_logging
from servicedesk.rag.answer import RagResult, answer_question, render_result, to_json
from servicedesk.rag.embedder import HashEmbedder
from servicedesk.rag.splitter import Chunk, split_markdown
from servicedesk.rag.store import QdrantStore, index_chunks

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"

HALLUCINATE_MARKER = "#hallucinate"

DEMO_QUESTIONS = [
    "VPN 提示认证失败怎么办",
    "邮件附件最大能发多大",
    "公司年会在哪里办",
    "VPN 认证失败怎么办 #hallucinate",
]


def header(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def load_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        chunks.extend(split_markdown(text, path.stem, max_chars=400, overlap_chars=80))
    return chunks


def verdict(result: RagResult) -> str:
    if result.no_evidence:
        return f"证据不足（rerank={result.evidence_score:.3f}）→ 没有调用模型（省钱且不会幻觉）"
    if not result.parse_ok:
        return "模型输出不是 JSON → 降级为纯文本，标记未接地"
    if result.rejected:
        return f"模型引用了不存在的编号 {result.dropped_citations} → 整段回答已作废"
    if not result.grounded:
        return "有答案但无有效引用 → 标记未接地，需人工复核"
    return f"已接地，引用 {len(result.citations)} 条，费用 ¥{result.cost_cny:.6f}"


async def build_index() -> tuple[QdrantStore, HashEmbedder]:
    chunks = load_chunks()
    embedder = HashEmbedder()
    store = QdrantStore(dim=embedder.dim)
    store.recreate()
    await index_chunks(store, chunks, embedder)
    print(f"索引就绪：{len(chunks)} 块，dim={embedder.dim}，库内 count={store.count()}")
    return store, embedder


async def ask_all(
    questions: list[str],
    *,
    top_k: int,
    min_rerank: float,
    as_json: bool,
) -> None:
    server, base_url = start_mock_server()
    settings = Settings(
        _env_file=None,
        llm_base_url=base_url,
        llm_model="mock-model",
        llm_api_key="mock-key",
    )
    try:
        store, embedder = await build_index()
        async with LLMClient(settings) as client:
            for index, question in enumerate(questions, start=1):
                header(f"问题 {index}：{question}")
                # 演示用：#hallucinate 只放进 prompt，不参与检索，
                # 否则标记词本身会稀释 rerank 分，连证据判定都过不了。
                instruction = HALLUCINATE_MARKER if HALLUCINATE_MARKER in question else ""
                clean_question = question.replace(HALLUCINATE_MARKER, "").strip()
                result = await answer_question(
                    client,
                    store,
                    embedder,
                    clean_question,
                    top_k=top_k,
                    rerank_min_score=min_rerank,
                    extra_instruction=instruction,
                )
                if as_json:
                    print(to_json(result))
                else:
                    print(render_result(result))
                    print()
                    print(f"→ 校验结论：{verdict(result)}")
    finally:
        server.shutdown()


async def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 问答（M1 验收工具）")
    parser.add_argument("question", nargs="?", help="要问的问题")
    parser.add_argument("--demo", action="store_true", help="跑四个预设场景")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-rerank", type=float, default=0.15, help="证据判定阈值（rerank 分）")
    args = parser.parse_args()

    configure_logging(level="ERROR")

    if args.demo or not args.question:
        questions = DEMO_QUESTIONS
    else:
        questions = [args.question]

    await ask_all(
        questions,
        top_k=args.top_k,
        min_rerank=args.min_rerank,
        as_json=args.json,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
