# file: scripts/split_demo.py
"""1.6 的场景：三组切分参数，三种检索命运。

不调用任何模型——用一个"字符二元组 + IDF"的极简检索器，
足够说明"块切坏了，检索必然出错"这件事。
（真正的向量检索在 1.7，这里不关心语义，只看字面。）

三组参数的差别只有两处：
    A vs B —— max_chars 变小，块被切碎
    B vs C —— 加上 overlap，跨块的一句话被保住

用法：
    uv run python -m scripts.split_demo
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from servicedesk.rag.splitter import Chunk, chunk_stats, split_markdown

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"

PARAM_SETS: dict[str, dict[str, int]] = {
    "A 切碎   max=120 overlap=0": {"max_chars": 120, "overlap_chars": 0, "min_chars": 60},
    "B 对齐   max=400 overlap=0": {"max_chars": 400, "overlap_chars": 0, "min_chars": 80},
    "C 加重叠 max=400 overlap=80": {"max_chars": 400, "overlap_chars": 80, "min_chars": 80},
}

QUERY = "VPN 提示认证失败怎么办"
KEYWORDS = ("VPN", "认证")
# 这个短语是"怎么办"的实际答案所在。切碎了就会和"原因"分家。
ANSWER_HINT = "重置密码"
BOUNDARY_END = re.compile(r"[。！？；：\n|）]\s*$")


def header(title: str) -> None:
    print()
    print("=" * 76)
    print(title)
    print("=" * 76)


def bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)}


class TinyRanker:
    """极简检索：二元组重合 + IDF 加权。

    加 IDF 是为了压掉「的」「我们」这类到处都是的字组——
    否则越长、越泛的块（比如文档开头那段介绍）总是排第一。
    """

    def __init__(self, chunks: list[Chunk], *, use_heading: bool) -> None:
        self.texts = [
            chunk.embedding_text if use_heading else chunk.text for chunk in chunks
        ]
        self.chunks = chunks
        self.grams = [bigrams(text) for text in self.texts]
        document_frequency: Counter[str] = Counter()
        for grams in self.grams:
            document_frequency.update(grams)
        self.df = document_frequency
        self.total = len(self.texts)

    def idf(self, gram: str) -> float:
        return math.log((self.total + 1) / (self.df.get(gram, 0) + 1)) + 1.0

    def search(self, query: str, k: int = 3) -> list[tuple[float, Chunk]]:
        query_grams = bigrams(query)
        denominator = sum(self.idf(gram) for gram in query_grams) or 1.0
        scored = []
        for grams, chunk in zip(self.grams, self.chunks):
            hit = sum(self.idf(gram) for gram in query_grams if gram in grams)
            scored.append((hit / denominator, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[:k]


def short(text: str, limit: int = 44) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def cut_boundaries(chunks: list[Chunk]) -> list[tuple[Chunk, Chunk]]:
    """找出"上一块没在句末结束"的相邻对——那就是被切断的句子。"""
    cuts = []
    for previous, current in zip(chunks, chunks[1:]):
        if previous.doc_id != current.doc_id:
            continue
        if not BOUNDARY_END.search(previous.text):
            cuts.append((previous, current))
    return cuts


def keyword_flag(chunk: Chunk, *, use_heading: bool) -> str:
    haystack = chunk.text
    if use_heading:
        haystack += " ".join(chunk.heading_path)
    hits = sum(1 for word in KEYWORDS if word in haystack)
    return "✅" if hits == len(KEYWORDS) else ("◐" if hits else "❌")


def show_search(chunks: list[Chunk], *, use_heading: bool) -> None:
    mode = "标题路径 + 正文" if use_heading else "仅正文"
    print(f"  检索「{QUERY}」（匹配{mode}）top3：")
    ranker = TinyRanker(chunks, use_heading=use_heading)
    for rank, (score, chunk) in enumerate(ranker.search(QUERY, 3), start=1):
        section = chunk.heading_path[-1] if chunk.heading_path else "（无标题）"
        print(
            f"    {rank}. {score:.3f} {keyword_flag(chunk, use_heading=use_heading)} "
            f"{chunk.chunk_id:<16} § {section}"
        )
        print(f"       {short(chunk.text)}")


def answer_in_top1(chunks: list[Chunk], *, use_heading: bool) -> bool:
    ranker = TinyRanker(chunks, use_heading=use_heading)
    top_score, top_chunk = ranker.search(QUERY, 1)[0]
    return ANSWER_HINT in top_chunk.text


def main() -> int:
    header("第 0 步：语料")
    docs: dict[str, str] = {}
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        docs[path.stem] = text
        print(f"  {path.name:<18} {len(text):>6} 字符")
    print(f"  合计 {len(docs)} 篇，{sum(len(t) for t in docs.values())} 字符")

    stats_table: dict[str, dict[str, float | int]] = {}
    cuts_table: dict[str, int] = {}
    answer_table: dict[str, str] = {}

    for name, params in PARAM_SETS.items():
        header(name)
        chunks: list[Chunk] = []
        for doc_id, text in docs.items():
            chunks.extend(split_markdown(text, doc_id, **params))

        stats = chunk_stats(chunks)
        stats_table[name] = stats
        print(
            f"  块数 {stats['count']}   平均 {stats['mean']} 字   "
            f"中位 {stats['p50']}   p90 {stats['p90']}   最大 {stats['max']}   "
            f"约 {stats['tokens_est']} tokens"
        )

        smallest = min(chunks, key=lambda c: c.char_len)
        missing = [w for w in KEYWORDS if w not in smallest.text]
        print(f"\n  最小的块（{smallest.char_len} 字）：{smallest.chunk_id}")
        print(f"    § {' > '.join(smallest.heading_path)}")
        print(f"    「{short(smallest.text, 58)}」")
        if missing:
            print(f"    → 正文里没有『{'、'.join(missing)}』：这就是『失去主语』的块")

        cuts = cut_boundaries(chunks)
        cuts_table[name] = len(cuts)
        print(f"\n  在句中断开的边界：{len(cuts)} 处")
        if cuts:
            previous, current = cuts[0]
            print(f"    例：{previous.chunk_id} 结尾「…{short(previous.text[-24:], 24)}」")
            print(f"        {current.chunk_id} 开头「{short(current.text[:24], 24)}…」")

        hit = answer_in_top1(chunks, use_heading=False)
        answer_table[name] = "是" if hit else "否"
        print()
        show_search(chunks, use_heading=False)
        print(
            f"  → top1 是否包含『怎么办』的实际答案（{ANSWER_HINT}）："
            f"{'✅ 是' if hit else '❌ 否，答案被切到下一块了'}"
        )
        print()
        show_search(chunks, use_heading=True)

    header("对比")
    print(
        f"  {'方案':<30}{'块数':>5}{'平均':>7}{'p90':>6}{'最大':>6}"
        f"{'tokens':>8}{'断句处':>7}{'top1含答案':>11}"
    )
    for name, stats in stats_table.items():
        print(
            f"  {name:<30}{stats['count']:>5}{stats['mean']:>7}"
            f"{stats['p90']:>6}{stats['max']:>6}{stats['tokens_est']:>8}"
            f"{cuts_table[name]:>7}{answer_table[name]:>11}"
        )

    header("小结")
    print("  A → B：max_chars 变大，块数从碎块变成小节块，但断句处依然存在")
    print("  B → C：块数相同，每块开头带上了上一块的结尾 → 跨块的一句话被保住")
    print()
    print("  两个反直觉结论：")
    print("  1. 决定块边界的第一因素不是 max_chars，而是文档的标题结构。")
    print("     小节比 max_chars 短时，调大 max_chars 毫无效果（B 与 C 块数完全相同）。")
    print("  2. 小块可以被标题路径救回来：对比『仅正文』与『标题路径 + 正文』的 top1。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
