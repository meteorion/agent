# file: tests/test_splitter.py
"""1.6 的断言：切分必须"不丢字、不破代码、带得走上下文"。"""

from __future__ import annotations

import pytest

from servicedesk.rag.splitter import (
    Chunk,
    blank_out_fences,
    chunk_stats,
    load_chunks,
    parse_sections,
    save_chunks,
    split_markdown,
)

DOC = """# 运维手册

手册开头的说明段落，用来测试无标题区间。

## 1. 安装

安装前请确认系统版本与磁盘空间。这一段是普通文字，长度足够构成一个独立段落。

```python
# 这一行长得像标题，但它不是
def hello() -> str:
    return "hi"
```

安装完成后请重启一次，否则驱动不生效。重启前请不要断电，否则可能损坏配置。

## 2. 配置

配置说明写在这里，同样是一段普通文字，用来验证小节切分是否正确。

### 2.1 详细参数

| 参数 | 含义 |
|---|---|
| a | 甲 |
| b | 乙 |
"""


def test_sections_carry_heading_path() -> None:
    sections = parse_sections(DOC)
    paths = [" > ".join(section.heading_path) for section in sections]
    assert "运维手册" in paths
    assert "运维手册 > 1. 安装" in paths
    assert "运维手册 > 2. 配置 > 2.1 详细参数" in paths


def test_heading_inside_fence_is_ignored() -> None:
    """代码块里的 `# 注释` 不能被当成标题。"""
    blanked = blank_out_fences(DOC)
    assert "# 这一行长得像标题" not in blanked

    paths = [" > ".join(s.heading_path) for s in parse_sections(DOC)]
    assert not any("长得像标题" in path for path in paths)


def test_chunk_offsets_point_at_exact_text() -> None:
    chunks = split_markdown(DOC, "doc", max_chars=120, overlap_chars=0)
    assert chunks
    for chunk in chunks:
        assert DOC[chunk.char_start : chunk.char_end].strip() == chunk.text


def test_code_fence_stays_atomic() -> None:
    """代码块不能被切开——切开的代码既检索不到也读不懂。"""
    chunks = split_markdown(DOC, "doc", max_chars=120, overlap_chars=0)
    fence_chunks = [chunk for chunk in chunks if "def hello" in chunk.text]
    assert len(fence_chunks) == 1, "整段代码应该落在同一个块里"
    text = fence_chunks[0].text
    assert text.count("```") == 2, "开合两个围栏都应该在同一个块里"
    assert "# 这一行长得像标题" in text


def test_section_smaller_than_max_becomes_one_chunk() -> None:
    """小节比 max_chars 短时，整节就是一个块——这是"小节对齐"的来源。"""
    chunks = split_markdown(DOC, "doc", max_chars=400, overlap_chars=0)
    section_chunks = [c for c in chunks if "重启一次" in c.text]
    assert len(section_chunks) == 1


def test_overlap_shares_text_with_previous_chunk() -> None:
    """同一小节被切开时，后一块的起点会回退到前一块的尾部。

    注意：overlap 只在**同一小节内部**生效。跨小节不加重叠，
    否则会把上一节的尾巴塞进下一节，反而制造污染。
    """
    with_overlap = split_markdown(DOC, "doc", max_chars=60, overlap_chars=20, min_chars=20)
    section_chunks = [
        chunk
        for chunk in with_overlap
        if chunk.heading_path and chunk.heading_path[-1] == "1. 安装"
    ]

    assert len(section_chunks) >= 2, "这个小节应该被切成多块"
    first, second = section_chunks[0], section_chunks[1]
    assert second.char_start < first.char_end, "两块应有字符区间重叠"
    assert second.text[:8] in first.text, "后一块的开头应该来自前一块的结尾"


def test_no_overlap_between_different_sections() -> None:
    """跨小节不加 overlap —— 每块都从自己的小节干净开始。"""
    chunks = split_markdown(DOC, "doc", max_chars=120, overlap_chars=40)
    for previous, current in zip(chunks, chunks[1:]):
        if previous.heading_path != current.heading_path:
            assert current.char_start >= previous.char_end


def test_max_chars_bounds_chunks_except_atomic_fence() -> None:
    chunks = split_markdown(DOC, "doc", max_chars=80, overlap_chars=0, min_chars=10)
    oversized = [c for c in chunks if c.char_len > 80]
    # 超长的只能是那个不可切的代码块
    assert all("```" in chunk.text for chunk in oversized)


def test_embedding_text_prepends_heading_path() -> None:
    chunks = split_markdown(DOC, "doc", max_chars=400, overlap_chars=0)
    chunk = next(c for c in chunks if "详细参数" in " ".join(c.heading_path))
    assert chunk.embedding_text.startswith("运维手册 > 2. 配置 > 2.1 详细参数")
    assert chunk.citation.startswith("doc § ")


def test_invalid_params_are_rejected() -> None:
    with pytest.raises(ValueError):
        split_markdown(DOC, "doc", max_chars=0)
    with pytest.raises(ValueError):
        split_markdown(DOC, "doc", max_chars=100, overlap_chars=100)


def test_save_and_load_roundtrip(tmp_path) -> None:
    chunks = split_markdown(DOC, "doc", max_chars=120, overlap_chars=0)
    path = tmp_path / "chunks.jsonl"
    save_chunks(chunks, path)

    restored = load_chunks(path)
    assert len(restored) == len(chunks)
    assert restored[0].model_dump() == chunks[0].model_dump()


def test_stats_report_spread() -> None:
    chunks = split_markdown(DOC, "doc", max_chars=120, overlap_chars=0)
    stats = chunk_stats(chunks)
    assert stats["count"] == len(chunks)
    assert stats["min"] <= stats["p50"] <= stats["max"]
    assert stats["tokens_est"] > 0
    assert chunk_stats([]) == {"count": 0}


def test_chunk_model_is_serializable() -> None:
    chunk = Chunk(
        chunk_id="doc#000",
        doc_id="doc",
        order=0,
        text="示例正文",
        heading_path=["手册"],
        char_start=0,
        char_end=4,
    )
    assert chunk.token_estimate >= 1
    assert chunk.char_len == 4
