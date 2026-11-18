# file: tests/test_async_demo.py
"""用断言把 1.2 的结论钉死：并发只对「等」有效，取消真的会停。

这些用例不打印任何东西，只看断言与耗时关系，适合放进 CI。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from scripts.async_demo import fetch


async def test_io_concurrent_is_faster_than_sequential() -> None:
    """两个 0.2s 的等待：并发应该明显短于顺序。"""
    start = time.perf_counter()
    await fetch("a", 0.2)
    await fetch("b", 0.2)
    sequential = time.perf_counter() - start

    start = time.perf_counter()
    await asyncio.gather(fetch("a", 0.2), fetch("b", 0.2))
    concurrent = time.perf_counter() - start

    assert sequential > 0.35, f"顺序本该约 0.4s，实测 {sequential:.3f}s"
    assert concurrent < sequential * 0.75, (
        f"并发 {concurrent:.3f}s 没有明显快于顺序 {sequential:.3f}s"
    )


async def test_timeout_fires_before_the_call_finishes() -> None:
    """超时必须在 1s 的调用结束前抛出，而不是傻等。"""
    start = time.perf_counter()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await fetch("slow", 1.0)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"超时用了 {elapsed:.3f}s，说明没有生效"


async def test_cancel_runs_finally_and_marks_cancelled() -> None:
    """被取消的任务必须执行 finally，且状态记为 cancelled。"""
    cleaned = {"done": False}

    async def job() -> None:
        try:
            await asyncio.sleep(5)
        finally:
            cleaned["done"] = True

    task = asyncio.create_task(job())
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleaned["done"] is True, "取消时 finally 没有执行，资源会泄漏"
    assert task.cancelled() is True
