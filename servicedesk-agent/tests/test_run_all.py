# file: tests/test_run_all.py
"""5.8 收尾：run_all 的清单必须与仓库真实文件一致。

为什么需要这条测试：`run_all.py` 里手写了 35 个模块名。
写错一个字母、或删掉一个 demo，它会在**发布前那条命令**上才报错。
这条测试把它提前到单元测试里。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from scripts.run_all import DEMOS, main


def test_every_demo_module_exists() -> None:
    missing = [demo.module for demo in DEMOS if importlib.util.find_spec(demo.module) is None]
    assert missing == [], f"模块不存在：{missing}"


def test_every_demo_file_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    missing = [
        demo.module for demo in DEMOS
        if not (root / (demo.module.replace(".", "/") + ".py")).exists()
    ]
    assert missing == []


def test_chapter_labels_are_unique_per_module() -> None:
    modules = [demo.module for demo in DEMOS]
    assert len(modules) == len(set(modules)), "同一个 demo 被登记了两次"


def test_chapter_labels_look_like_sections() -> None:
    for demo in DEMOS:
        parts = demo.chapter.split(".")
        assert len(parts) == 2 and all(part.isdigit() for part in parts), demo.chapter


def test_descriptions_are_present() -> None:
    assert all(demo.description.strip() for demo in DEMOS)


def test_slow_demos_are_marked() -> None:
    """两个十几秒的 demo 必须标 slow，否则 --fast 失去意义。"""
    slow = {demo.module for demo in DEMOS if demo.slow}
    assert "scripts.streaming_demo" in slow
    assert "scripts.async_demo" in slow


def test_list_mode_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "将运行" in out
    assert "scripts.m2_agent" in out


def test_list_mode_with_fast_skips_slow(capsys: pytest.CaptureFixture[str]) -> None:
    main(["--list", "--fast"])
    out = capsys.readouterr().out
    assert "streaming_demo" not in out
    assert "跳过耗时 demo" in out


def test_expected_coverage() -> None:
    """35 个 demo 覆盖 37 节里的 35 节（3.2/4.x 部分节共用或无需 demo）。"""
    assert len(DEMOS) == 35
