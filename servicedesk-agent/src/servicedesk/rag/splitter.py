# file: src/servicedesk/rag/splitter.py
"""把长文档切成"检索友好"的块。

三个设计决定，每一个都对应一类真实事故：
1. 按**标题层级**切分并保留 heading_path —— 否则小块会失去主语，检索时到处乱命中
2. 代码块/表格**保持原子** —— 否则切开的代码既检索不到也读不懂
3. 相邻块留 **overlap** —— 否则一句话的答案被切在两个块中间，谁也答不全
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

# 大模型能识别的标题行：## 这样
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 句子边界：中文句末标点 + 换行
SENTENCE_RE = re.compile(r"[^。！？；\n]+[。！？；\n]?")
# 围栏代码块（``` 开头到 ``` 结束）
FENCE_RE = re.compile(r"```[^\n]*\n.*?```", re.S)


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：中文约 1.5 字符 / token。

    真实值必须用 tokenizer 数。这里只是为了让"成本大致是多少"有个数，
    别拿它去算账单。
    """
    return max(1, round(len(text) / 1.5))


# -------------------------------- 数据模型 --------------------------------


class Chunk(BaseModel):
    """一个可检索、可引用、可回溯原文的片段。"""

    chunk_id: str = Field(description="全局唯一，形如 vpn-guide#003")
    doc_id: str = Field(description="来源文档标识")
    order: int = Field(description="在本文档内的顺序，从 0 开始")
    text: str = Field(description="原始片段文本（不含标题）")
    heading_path: list[str] = Field(
        default_factory=list, description="从一级标题到当前小节的路径"
    )
    char_start: int = Field(ge=0, description="在原文中的起始字符偏移")
    char_end: int = Field(ge=0, description="在原文中的结束字符偏移")

    @property
    def char_len(self) -> int:
        return len(self.text)

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.text)

    @property
    def embedding_text(self) -> str:
        """真正拿去算向量的文本。

        把小节路径拼在前面，等于给块补回"主语"。
        实测这一个改动就能显著改善小块的可检索性（见 §6.1）。
        """
        if not self.heading_path:
            return self.text
        return " > ".join(self.heading_path) + "\n" + self.text

    @property
    def citation(self) -> str:
        """可以直接塞进回答里的引用标注。"""
        path = " > ".join(self.heading_path) or "（无标题）"
        return f"{self.doc_id} § {path}"


@dataclass(frozen=True)
class _Section:
    """一个标题下的正文区间（不含标题行本身）。"""

    heading_path: list[str]
    start: int
    end: int


# -------------------------------- 解析 --------------------------------


def blank_out_fences(text: str) -> str:
    """把围栏代码块替换成等长的 NUL 字符。

    为什么这么做（而不是删除）：**替换成等长**能让字符偏移完全不变，
    后面所有 span 仍能直接用来切原文。同时代码块里没有换行符，
    它就不会被段落切分规则拆开。
    """
    return FENCE_RE.sub(lambda match: "\x00" * (match.end() - match.start()), text)


def parse_sections(md: str) -> list[_Section]:
    """按标题把文档切成若干 section，并记录每一段的标题路径。"""
    blanked = blank_out_fences(md)
    sections: list[_Section] = []

    stack: list[tuple[int, str]] = []     # (level, title)
    path_now: list[str] = []
    body_start = 0
    offset = 0

    for line in blanked.splitlines(keepends=True):
        heading = HEADING_RE.match(line.rstrip("\n"))
        if heading:
            if offset > body_start:
                sections.append(_Section(list(path_now), body_start, offset))
            level = len(heading.group(1))
            title = heading.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            path_now = [name for _, name in stack]
            body_start = offset + len(line)
        offset += len(line)

    if len(blanked) > body_start:
        sections.append(_Section(list(path_now), body_start, len(blanked)))
    return sections


def _paragraph_spans(blanked: str) -> list[tuple[int, int]]:
    """按空行切段落，返回字符区间。"""
    spans: list[tuple[int, int]] = []
    position = 0
    for part in blanked.split("\n\n"):
        start = position
        end = position + len(part)
        if end > start:
            spans.append((start, end))
        position = end + 2
    return spans


def _atomic_spans(blanked: str, max_unit: int) -> list[tuple[int, int]]:
    """把正文拆成"不可再小"的单元：段落 → 句子 → 硬切。

    代码块因为已被 blank_out_fences 抹平（内部无换行、无句末标点），
    会自然地作为一个整体留下来，不会被切开。
    """
    units: list[tuple[int, int]] = []

    for start, end in _paragraph_spans(blanked):
        if end - start <= max_unit:
            units.append((start, end))
            continue

        paragraph = blanked[start:end]
        sentences = [
            (start + match.start(), start + match.end())
            for match in SENTENCE_RE.finditer(paragraph)
            if match.end() > match.start()
        ]
        if len(sentences) <= 1:
            # 没有句子边界（典型的整块代码/表格）→ 保持原子，宁可超长
            units.append((start, end))
            continue

        for sentence_start, sentence_end in sentences:
            if sentence_end - sentence_start <= max_unit:
                units.append((sentence_start, sentence_end))
            else:
                for cut in range(sentence_start, sentence_end, max_unit):
                    units.append((cut, min(cut + max_unit, sentence_end)))

    return units


def _pack(
    spans: list[tuple[int, int]],
    max_chars: int,
    overlap_chars: int,
) -> list[tuple[int, int]]:
    """把单元贪心地装进 chunk，并给相邻 chunk 之间加上重叠。"""
    if not spans:
        return []

    packed: list[list[int]] = []
    current_start, current_end = spans[0]

    for start, end in spans[1:]:
        if end - current_start <= max_chars:
            current_end = end
            continue
        packed.append([current_start, current_end])
        new_start = current_end - overlap_chars if overlap_chars else start
        new_start = max(0, min(new_start, start))
        current_start, current_end = new_start, end

    packed.append([current_start, current_end])
    return [(start, end) for start, end in packed]


def _merge_tiny(
    spans: list[tuple[int, int]],
    min_chars: int,
) -> list[tuple[int, int]]:
    """把过短的块并进前一块，避免出现"只有一行字"的碎片。"""
    if not spans:
        return []

    merged: list[list[int]] = [list(spans[0])]
    for start, end in spans[1:]:
        previous = merged[-1]
        too_short = (end - start) < min_chars or (previous[1] - previous[0]) < min_chars
        if too_short:
            previous[1] = max(previous[1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


# -------------------------------- 对外接口 --------------------------------


def split_markdown(
    md: str,
    doc_id: str,
    *,
    max_chars: int = 500,
    overlap_chars: int = 80,
    min_chars: int = 80,
) -> list[Chunk]:
    """把一篇 Markdown 切成 Chunk 列表。

    参数意义：
        max_chars     —— 目标块大小（超长的代码块会突破它，这是有意的）
        overlap_chars —— 相邻块的重叠长度，保证跨块的一句话不被切断
        min_chars     —— 小于它的碎片会被并入前一块
    """
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正数")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars 必须 >= 0 且小于 max_chars")
    if min_chars >= max_chars:
        # 否则 _merge_tiny 会把每一块都判定为"太短"，全部合并回一整块，
        # 参数看起来生效了，实际完全没切
        raise ValueError("min_chars 必须小于 max_chars")

    blanked = blank_out_fences(md)
    sections = parse_sections(md)

    chunks: list[Chunk] = []
    order = 0

    for section in sections:
        body = blanked[section.start : section.end]
        if not body.strip():
            continue

        units = _atomic_spans(body, max_chars)
        packed = _pack(units, max_chars, overlap_chars)
        packed = _merge_tiny(packed, min_chars)

        for start, end in packed:
            text = md[section.start + start : section.start + end].strip()
            if not text:
                continue
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}#{order:03d}",
                    doc_id=doc_id,
                    order=order,
                    text=text,
                    heading_path=list(section.heading_path),
                    char_start=section.start + start,
                    char_end=section.start + end,
                )
            )
            order += 1

    return chunks


def split_file(path: Path, *, doc_id: str | None = None, **kwargs: int) -> list[Chunk]:
    """从 .md 文件切分；doc_id 默认用文件名（不含扩展名）。"""
    text = path.read_text(encoding="utf-8")
    return split_markdown(text, doc_id or path.stem, **kwargs)  # type: ignore[arg-type]


def save_chunks(chunks: list[Chunk], path: Path) -> None:
    """按 JSONL 落盘：一行一个 chunk，方便流式读写与 diff。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(chunk.model_dump_json() + "\n")


def load_chunks(path: Path) -> list[Chunk]:
    with path.open("r", encoding="utf-8") as handle:
        return [Chunk.model_validate_json(line) for line in handle if line.strip()]


def chunk_stats(chunks: list[Chunk]) -> dict[str, float | int]:
    """切分质量的体检指标。"""
    if not chunks:
        return {"count": 0}

    lengths = sorted(chunk.char_len for chunk in chunks)
    return {
        "count": len(chunks),
        "total_chars": sum(lengths),
        "min": lengths[0],
        "max": lengths[-1],
        "mean": round(statistics.mean(lengths), 1),
        "p50": lengths[len(lengths) // 2],
        "p90": lengths[min(int(len(lengths) * 0.9), len(lengths) - 1)],
        "tokens_est": sum(chunk.token_estimate for chunk in chunks),
    }


def dumps_chunks(chunks: list[Chunk]) -> str:
    """调试用：把 chunk 列表打成可读 JSON。"""
    return json.dumps([chunk.model_dump() for chunk in chunks], ensure_ascii=False, indent=2)
