# file: scripts/async_demo.py
"""1.2 的五个实验：并发、CPU 型阻塞、超时、取消、异常聚合。

用法：
    uv run python scripts/async_demo.py all       # 全部跑一遍
    uv run python scripts/async_demo.py io        # I/O 型：顺序 vs 并发
    uv run python scripts/async_demo.py cpu       # CPU 型：说明并发≠提速
    uv run python scripts/async_demo.py timeout   # 给等待设上限
    uv run python scripts/async_demo.py cancel    # 取消后任务真的停了
    uv run python scripts/async_demo.py group     # 一个失败，全部连坐

所有"耗时"都用 asyncio.sleep 或纯计算模拟，不联网、不花钱。
"""

from __future__ import annotations

import argparse
import asyncio
import time

DELAY_S = 0.5  # 模拟一次远程调用的耗时
ITERATIONS = 5_000_000  # 一次 CPU 任务的固定工作量


def header(title: str) -> None:
    print()
    print("=" * 62)
    print(title)
    print("=" * 62)


# --------------------------- 被调用的「远程服务」 ---------------------------


async def fetch(label: str, delay: float = DELAY_S) -> str:
    """模拟一次网络请求：等 delay 秒，返回结果。

    关键点：await 期间协程把控制权交还事件循环，
    别的协程可以趁机干活。这就是「并发」的全部秘密。
    """
    await asyncio.sleep(delay)
    return f"{label}-ok"


async def fetch_with_cleanup(label: str, delay: float = 2.0) -> str:
    """带资源清理的请求，用来观察「取消」时 finally 是否执行。"""
    print(f"  [{label}] 开始，预计 {delay}s")
    try:
        await asyncio.sleep(delay)
        print(f"  [{label}] 正常完成")
        return f"{label}-ok"
    finally:
        # 无论是正常结束、抛异常还是被取消，这里都会执行
        print(f"  [{label}] finally: 释放连接")


def busy(iterations: int) -> int:
    """纯 CPU 计算：固定工作量，期间不会交还控制权。

    注意这里用「固定迭代次数」而不是「固定秒数」：
    如果按时间忙等，多线程会同时到点，看起来反而更快，实验就假了。
    """
    total = 0
    for i in range(iterations):
        total += (i * i) % 7
    return total


# ------------------------------- 实验 1：I/O -------------------------------


async def io_sequential() -> float:
    """顺序等三次。"""
    start = time.perf_counter()
    results = [await fetch(f"req{i}") for i in range(1, 4)]
    elapsed = time.perf_counter() - start
    print(f"  结果: {results}")
    print(f"  顺序耗时: {elapsed:.3f}s")
    return elapsed


async def io_concurrent() -> float:
    """同时等三次。"""
    start = time.perf_counter()
    results = await asyncio.gather(fetch("req1"), fetch("req2"), fetch("req3"))
    elapsed = time.perf_counter() - start
    print(f"  结果: {list(results)}")
    print(f"  并发耗时: {elapsed:.3f}s")
    return elapsed


async def demo_io() -> None:
    header("实验 1：I/O 型任务 —— 顺序 vs 并发")
    seq = await io_sequential()
    con = await io_concurrent()
    print(f"\n  >>> 快 {seq / con:.2f} 倍")
    print("  >>> 注意：快了约 3 倍，但没有任何一步「算」得更快，只是「等」重叠了。")


# ---------------------------- 实验 2：CPU 型 ----------------------------


async def cpu_sequential() -> float:
    start = time.perf_counter()
    for _ in range(3):
        busy(ITERATIONS)
    elapsed = time.perf_counter() - start
    print(f"  顺序跑 3 次耗时: {elapsed:.3f}s")
    return elapsed


async def cpu_concurrent() -> float:
    start = time.perf_counter()
    # 看起来是「并发」，但 GIL 决定了同一时刻只有一个线程执行 Python 字节码，
    # 所以 3 份纯 Python 计算仍只能轮流跑，总墙钟时间不会缩短。
    await asyncio.gather(
        asyncio.to_thread(busy, ITERATIONS),
        asyncio.to_thread(busy, ITERATIONS),
        asyncio.to_thread(busy, ITERATIONS),
    )
    elapsed = time.perf_counter() - start
    print(f"  gather + to_thread 耗时: {elapsed:.3f}s")
    return elapsed


async def demo_cpu() -> None:
    header("实验 2：CPU 型任务 —— 并发不会更快（并发 ≠ 并行）")
    seq = await cpu_sequential()
    con = await cpu_concurrent()
    print(f"\n  >>> 顺序 {seq:.3f}s / 并发 {con:.3f}s")
    print("  >>> 没有加速：CPU 计算期间事件循环无法切换，没有任何人在「等」。")


# ----------------------------- 实验 3：超时 -----------------------------


async def demo_timeout() -> None:
    header("实验 3：给等待设上限（asyncio.timeout）")
    start = time.perf_counter()
    try:
        # 最多允许 0.5 秒；超了就抛 TimeoutError，而不是无限等下去
        async with asyncio.timeout(0.5):
            await fetch("slow-api", delay=2.0)
        print("  不该走到这里")
    except TimeoutError:
        elapsed = time.perf_counter() - start
        print(f"  捕获 TimeoutError，用时 {elapsed:.3f}s（而不是 2.0s）")
        print("  >>> 生产里所有外部调用都必须有这一层。")


# ----------------------------- 实验 4：取消 -----------------------------


async def demo_cancel() -> None:
    header("实验 4：取消之后，任务是真的停了")
    task = asyncio.create_task(fetch_with_cleanup("long-job", delay=2.0))
    await asyncio.sleep(0.3)
    print("  0.3s 后决定不要了，调用 cancel()")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        print("  主流程收到 CancelledError，确认任务已终止")
    print(f"  任务状态: cancelled={task.cancelled()}")
    print("  >>> 两秒后没有出现「正常完成」，说明它没在后台偷偷跑完。")


# -------------------------- 实验 5：异常聚合 --------------------------


async def demo_group() -> None:
    header("实验 5：TaskGroup —— 一个失败，其余连坐取消")

    async def ok() -> str:
        await asyncio.sleep(2.0)  # 本该跑 2 秒
        return "ok"

    async def boom() -> str:
        await asyncio.sleep(0.2)
        raise RuntimeError("模拟下游 500")

    start = time.perf_counter()
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(ok())
            tg.create_task(boom())
    except* RuntimeError as eg:
        elapsed = time.perf_counter() - start
        print(f"  捕获 ExceptionGroup，{len(eg.exceptions)} 个错误：{eg.exceptions}")
        print(f"  总耗时 {elapsed:.3f}s —— 没有傻等那 2 秒")
        print("  >>> 兄弟任务被自动取消，这是 TaskGroup 比裸 gather 强的地方。")


# -------------------------------- 入口 --------------------------------


async def run_all() -> None:
    await demo_io()
    await demo_cpu()
    await demo_timeout()
    await demo_cancel()
    await demo_group()


COMMANDS = {
    "io": demo_io,
    "cpu": demo_cpu,
    "timeout": demo_timeout,
    "cancel": demo_cancel,
    "group": demo_group,
    "all": run_all,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="async 心智模型实验")
    parser.add_argument("command", nargs="?", default="all", choices=sorted(COMMANDS))
    args = parser.parse_args()
    asyncio.run(COMMANDS[args.command]())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
