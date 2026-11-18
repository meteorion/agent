---
section: "1.2"
title: "用「点餐叫号」理解 async：Agent 为什么必须异步"
stage: 1
status: done
depth: L2
difficulty: 2
minutes: 75
milestone: M1
depends_on: ["1.1"]
produces: ["scripts/async_demo.py"]
verifies: "能解释「快 3 倍」的真实原因，并演示取消后任务确实停了"
part: null
updated: "2026-11-17"
---

# 1.2 用「点餐叫号」理解 async：Agent 为什么必须异步

## 0. 本节导航

**本节在链路中的位置**：接 1.1 的工程骨架，给后面的**所有**代码打上"异步"这个底子。1.3 要用异步 HTTP 调 LLM，2.x 的 MCP 客户端是异步的，3.x 的 LangGraph 也是异步的。这一节没吃透，后面每一节都会疼。

**本节你将获得**

1. 用一句人话解释"并发"到底是什么，以及它**不能**解决什么。
2. 亲手测出两个数字：I/O 型并发快约 3 倍，CPU 型并发**一点不快**。
3. 用 `asyncio.timeout` 给任何等待设上限，不再无限等。
4. 演示"取消后任务真的停了"，并说明为什么 `finally` 必须写。
5. 用 `TaskGroup` 做到"一个失败，其余连坐"，而不是让异常被悄悄吞掉。

**前置检查清单**

- 1.1 的 `uv run python -m servicedesk` 能打出 `✓ 环境自检通过`。
- 能在 `servicedesk-agent/` 目录下执行 `uv run`。

**难度**：★★☆☆☆ ｜ **预计时长**：75 分钟 ｜ **本节费用**：**¥0**（全部用 `asyncio.sleep` 与纯计算模拟，不联网）

---

## 1. 为什么需要它（痛点先行）

你要做一个工单 Agent，用户提一个问题，后台需要：

1. 检索知识库（远程向量库，约 300ms）
2. 查资产系统（HTTP 接口，约 400ms）
3. 查权限系统（HTTP 接口，约 300ms）

三件事**互不依赖**。写成同步代码：

```python
kb = retrieve_knowledge(q)   # 0.3s
asset = query_asset(q)       # 0.4s
perm = query_perm(q)         # 0.3s
```

总耗时 **1.0 秒**。用户等了一秒，但这一秒里，你的程序 **99.99% 的时间在发呆**——它在等网络回包，CPU 几乎空闲。

如果这个系统一天处理 10 万次请求，按同步写法，你要么开 10 万个线程（内存先炸），要么让用户排队。这就是"玩具 Demo"和"生产系统"的第一道分水岭。

**更糟的情况**：LLM 一次生成要 3–10 秒。三个工具调用串起来，用户要盯着转圈 15 秒。而如果把"等"重叠起来，只需要最慢那一个的时间。

异步要解决的就是这一件事：**别在等的时候发呆。**

---

## 2. 核心概念与架构设计

### 2.1 类比：点餐叫号

你去快餐店点餐：

- **同步（阻塞）**：你站在窗口前，盯着厨师做，做好拿走，然后才轮到下一个人。队伍越排越长。
- **异步（非阻塞）**：你把单子递给柜台，拿一个小票号，**走到旁边坐着**。后厨做好会叫号。你在等的这段时间，可以再点一杯饮料，也可以帮朋友点一份。

关键观察：**你从头到尾只有一个"你"**（单线程），但你在"等"的时候可以处理别的事。这就是并发的全部秘密。

> **这个类比在哪失效**：如果厨师只有一口锅，且每道菜都要他"亲自动手切菜"（CPU 计算），那你点三份也不会更快——只能一份一份做。**"等"能重叠，"算"不能。** 这一点是本节的核心，第 6 节会实验给你看。

### 2.2 用时间轴看清差别

```
顺序执行（同步）总耗时 1.5s
req1  ├────────┤
req2           ├────────┤
req3                    ├────────┤
      0        0.5      1.0      1.5 s
              ▲ 这 1.0s 里 CPU 基本在发呆

并发执行（异步）总耗时 0.5s
req1  ├────────┤
req2  ├────────┤
req3  ├────────┤
      0        0.5
              ▲ 三个"等"完全重叠
```

### 2.3 五个名词，一次讲清

| 名词 | 是什么 | 类比 |
|---|---|---|
| **协程**（coroutine） | 用 `async def` 定义的函数，调用它不会立刻执行，返回一个"待执行的东西" | 一张**还没递出去**的单子 |
| **事件循环**（event loop） | 一个循环，负责调度所有协程，谁可以跑就让谁跑 | 后厨的**叫号系统** |
| **`await`** | "我要等了，先把位置让出来" | 你把单子递进窗口，**走到旁边坐下** |
| **任务**（Task） | 被事件循环正式受理、可以独立跑完的协程 | **已经进后厨**的那份单 |
| **取消**（cancel） | 告诉任务"不要了" | 你**退单**，后厨停手并丢掉半成品 |

### 2.4 逐层拆解：`await` 那一刻到底发生了什么

```
你的协程执行到  await asyncio.sleep(0.5)
        │
        ├─ 1. 协程把控制权交还给事件循环（yield）
        ├─ 2. 事件循环在"0.5 秒后叫醒它"的名单里登记一条
        ├─ 3. 事件循环转去跑别的就绪协程 ← 这里产生了并发
        └─ 4. 0.5 秒到了，事件循环回来，从 yield 处继续执行
```

**重点：切换发生在 `await` 处，且只在 `await` 处。** 如果你在协程里写了一行同步的 `time.sleep(0.5)`，事件循环根本没有机会切走——它被"卡住"了。这是第 8 节坑 1 的根因。

### 2.5 适用范围（边界）

| 场景 | 异步有用吗 | 原因 |
|---|---|---|
| 调 HTTP / 数据库 / 向量库 / LLM | ✅ 大幅提升 | 时间花在"等"上 |
| 读大文件、等磁盘 | ✅ 有用 | 同上 |
| 纯计算（加密、图像处理、大量字符串处理） | ❌ 没用 | 时间花在"算"上，无法交还控制权 |
| 单个任务、无并发需求 | ⚠️ 无所谓 | 异步只是多了层语法 |

> **判断口诀**：先问"这段时间 CPU 在干嘛"。**在发呆 → 异步有用；在算 → 异步没用，该上多进程。**

---

## 3. 环境与版本

本节不需要新依赖，1.1 的环境就够：

| 组件 | 实测版本 | 说明 |
|---|---|---|
| Python | `3.12.13` | `asyncio.timeout` / `TaskGroup` / `except*` 需要 **≥3.11** |
| pytest-asyncio | `1.5.0` | 1.1 已装进 dev 组，本节终于用上 |

**新增一处配置**：让测试能 import 项目内模块（`scripts/` 不在 `src/` 里）。在 `pyproject.toml` 的 `[tool.pytest.ini_options]` 加一行：

```toml
pythonpath = ["."]        # 让 tests 能 import scripts.* 这类项目内模块
```

改完必须 `uv sync`（第 1.1 节的规矩：改了 `pyproject.toml` 就要 sync，uv 会自动重新构建你的包）。

**验证装对了**

```powershell
cd servicedesk-agent
uv run python -c "import asyncio, sys; print(sys.version.split()[0], hasattr(asyncio, 'TaskGroup'))"
```

预期：

```
3.12.13 True
```

---

## 4. 代码实战（2 个文件）

完整实现见 [scripts/async_demo.py](../../servicedesk-agent/scripts/async_demo.py)，测试见 [tests/test_async_demo.py](../../servicedesk-agent/tests/test_async_demo.py)。

### 文件 1 / 2：`scripts/async_demo.py`

```python
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

DELAY_S = 0.5            # 模拟一次远程调用的耗时
ITERATIONS = 5_000_000   # 一次 CPU 任务的固定工作量


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
```

### 文件 2 / 2：`tests/test_async_demo.py`

```python
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
```

### 运行

```powershell
uv run python scripts/async_demo.py all
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 `async def` 只是"定义"，不执行

```python
async def fetch(...) -> str:
    ...
```

调用 `fetch("a")` **不会**发出请求，它返回一个**协程对象**——一张还没递出去的单子。必须 `await` 它，或者交给事件循环（`gather` / `create_task`）。

如果你直接调用不 `await`，Python 会警告：

```
RuntimeWarning: coroutine 'fetch' was never awaited
```

**这条警告必须当成错误看待**，因为它的后果是"逻辑静默没执行"——见第 8 节坑 2。

### 5.2 `gather` 为什么能快 3 倍

```python
await asyncio.gather(fetch("req1"), fetch("req2"), fetch("req3"))
```

三件事依次发生：

1. `gather` 把三个协程**同时**提交给事件循环。
2. 三个都跑到 `await asyncio.sleep(0.5)`，各自"让位"。
3. 事件循环手里同时有三个"0.5 秒后叫醒"的登记，0.5 秒一到，三个几乎同时恢复。

所以总耗时 ≈ **最慢那个**（0.5s），而不是**求和**（1.5s）。实测 `2.96 倍`，略低于 3，因为调度本身有微小开销。

### 5.3 实验 2 为什么"一点没快"——本节的"啊哈点"

**并发 ≠ 并行。**

- **并发（concurrency）**：多个任务**交替推进**。单线程就能做到，靠的是"在等的时候切走"。
- **并行（parallelism）**：多个任务**真正同时**在多个 CPU 核上执行。需要多进程。

实验 2 里三份 `busy()` 是纯计算，**没有任何 `await`，也就没有任何"等"**。事件循环没有机会切走，只能一个算完再算下一个。所以 `1.547s` vs `1.554s`——一模一样。

有人会想"用 `asyncio.to_thread` 开线程不就行了？"实测同样 `1.554s`。原因是 **GIL**：同一时刻只允许一个线程执行 Python 字节码，纯 Python 计算在线程间仍然串行。

> **判断口诀**：I/O 密集 → 异步；CPU 密集 → 多进程（`ProcessPoolExecutor`）。**异步不是万能加速器，它只加速"等"。**

### 5.4 `asyncio.timeout`：把"无限等"变成"最多等 N 秒"

```python
async with asyncio.timeout(0.5):
    await fetch("slow-api", delay=2.0)
```

超时后抛的是**内置的 `TimeoutError`**（3.11 起），不再是旧版的 `asyncio.TimeoutError`。这点在写 `except` 时很重要。

实测：`2.0s` 的调用在 `0.501s` 就抛出了。**这 1.5 秒是你从用户那里抢回来的。**

### 5.5 取消：为什么 `finally` 是保命的

```python
try:
    await asyncio.sleep(delay)
finally:
    print("释放连接")
```

`asyncio.CancelledError` 不是普通异常，但它**依然会走 `finally`**。实测输出顺序值得注意：

```
[long-job] 开始，预计 2.0s
0.3s 后决定不要了，调用 cancel()
[long-job] finally: 释放连接      ← 清理先发生
主流程收到 CancelledError         ← 然后才到主流程
```

没有 `finally`，你取消掉的那个 LLM 流式请求、那条数据库连接、那个临时文件，**全都留在原地泄漏**。长跑的服务几天后就 OOM。

### 5.6 `TaskGroup` vs 裸 `gather`：差在"连坐"和"聚合"

| 维度 | `asyncio.gather` | `asyncio.TaskGroup` |
|---|---|---|
| 一个任务失败 | 默认只把第一个异常抛出来，其余继续跑 | **自动取消其余任务**，一起收尾 |
| 多个任务同时失败 | 只看到第一个 | 抛 `ExceptionGroup`，全部可见 |
| 异常捕获语法 | `except` | `except*`（3.11+） |
| 适用 | 快速脚本、确定不会相互影响 | 生产代码，默认选择 |

实测：一个任务 2 秒、一个任务 0.2 秒就炸，`TaskGroup` 在 **0.206s** 就返回了，没有傻等那 2 秒。

> **默认建议**：新代码一律用 `TaskGroup`。它替你做了"失败要收尾"这件最容易忘记的事。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：把"固定工作量"改成"按时间忙等"，结论就反了

把 `busy()` 改成按**时间**忙等：

```python
def busy_wall(seconds: float) -> int:
    end = time.perf_counter() + seconds
    n = 0
    while time.perf_counter() < end:   # ← 问题在这里
        n += 1
    return n
```

然后用同样的方式跑顺序 vs 并发。实测：

```
按时间忙等: 顺序 0.750s / 并发 0.368s  -> 看起来快了 2.04 倍
```

**结论变成了"CPU 任务并发也能快 2 倍"——这是假的。**

原因很隐蔽：三个线程各自看表，都定在"0.25 秒后停"。GIL 让它们轮流执行，但**每个线程的"到点"是按墙钟判断的**，所以它们几乎同时退出。看起来 0.25s 干完了 0.75s 的活，实际每个线程都少算了。

**这个坑的真实危害不在教程里，在你的压测报告里。** 用"跑 N 秒"来度量吞吐，会把限流、排队、降级的效果全部涂平，得出"加了并发就快了 3 倍"的假结论。**度量 CPU 任务要用固定工作量（迭代数/请求数），不要用固定时长。**

### 6.2 实验 B：在协程里写同步阻塞调用，并发直接失效

把 `fetch()` 里的 `await asyncio.sleep(delay)` 换成 `time.sleep(delay)`（或者换成 `requests.get(...)`，效果一样）：

```python
async def bad_call(label: str, d: float = 0.5) -> str:
    time.sleep(d)          # 同步阻塞，事件循环被卡死
    return label
```

三个并发调用，实测：

```
time.sleep 版并发耗时: 1.501s
```

**1.5 秒，一点没省。** 因为 `time.sleep` 不 `await`，事件循环从头到尾没有机会切走——它被这个调用按住不动了。`requests`、`psycopg2`（同步模式）、`open()` 读大文件，全是同一类问题。

**修法**：换异步库（`httpx` / `asyncpg` / `aiofiles`）；确实只有同步库时，用 `await asyncio.to_thread(func, ...)` 把它挪到线程里，别堵事件循环。

---

## 7. 验证与预期输出

以下为本机实测（Python 3.12.13 / Windows 11）。

```powershell
uv run python scripts/async_demo.py all
```

```
==============================================================
实验 1：I/O 型任务 —— 顺序 vs 并发
==============================================================
  结果: ['req1-ok', 'req2-ok', 'req3-ok']
  顺序耗时: 1.525s
  结果: ['req1-ok', 'req2-ok', 'req3-ok']
  并发耗时: 0.515s

  >>> 快 2.96 倍
  >>> 注意：快了约 3 倍，但没有任何一步「算」得更快，只是「等」重叠了。

==============================================================
实验 2：CPU 型任务 —— 并发不会更快（并发 ≠ 并行）
==============================================================
  顺序跑 3 次耗时: 1.547s
  gather + to_thread 耗时: 1.554s

  >>> 顺序 1.547s / 并发 1.554s
  >>> 没有加速：CPU 计算期间事件循环无法切换，没有任何人在「等」。

==============================================================
实验 3：给等待设上限（asyncio.timeout）
==============================================================
  捕获 TimeoutError，用时 0.501s（而不是 2.0s）
  >>> 生产里所有外部调用都必须有这一层。

==============================================================
实验 4：取消之后，任务是真的停了
==============================================================
  [long-job] 开始，预计 2.0s
  0.3s 后决定不要了，调用 cancel()
  [long-job] finally: 释放连接
  主流程收到 CancelledError，确认任务已终止
  任务状态: cancelled=True
  >>> 两秒后没有出现「正常完成」，说明它没在后台偷偷跑完。

==============================================================
实验 5：TaskGroup —— 一个失败，其余连坐取消
==============================================================
  捕获 ExceptionGroup，1 个错误：(RuntimeError('模拟下游 500'),)
  总耗时 0.206s —— 没有傻等那 2 秒
  >>> 兄弟任务被自动取消，这是 TaskGroup 比裸 gather 强的地方。
```

**三个数字是本节的全部结论**：`2.96 倍`（I/O 有效）、`1.554s`（CPU 无效）、`0.501s`（超时生效）。

跑测试：

```powershell
uv run pytest
```

```
......                                                                   [100%]
6 passed in 0.95s
```

（1.1 的 3 个 + 本节的 3 个。）

---

## 8. 生产环境踩坑实录

### 坑 1：异步代码跑起来跟同步一样慢

**现象**

服务接上了"异步框架"，QPS 一点没涨，P99 还是 1.5 秒。看 CPU 使用率却很低（说明不是在算）。

**定位方法**

1. 搜代码里所有不带 `await` 的外部调用：

```powershell
Select-String -Path src\**\*.py -Pattern 'time\.sleep|requests\.|\.execute\('
```

2. 在事件循环里装一个"心跳"，看它是不是卡住了：

```python
async def heartbeat() -> None:
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(0.1)
        drift = time.perf_counter() - t0 - 0.1
        if drift > 0.05:
            print(f"事件循环被堵了 {drift * 1000:.0f}ms")
```

**根因**

协程里混入了同步阻塞调用（`time.sleep` / `requests` / 同步 DB 驱动 / 大文件同步读）。它不 `await`，事件循环无法切走，整个进程退化成串行执行。

**修法**

```diff
- def fetch_user(uid: str) -> dict:
-     return requests.get(f"{BASE}/users/{uid}", timeout=5).json()
+ async def fetch_user(uid: str) -> dict:
+     async with httpx.AsyncClient(timeout=5) as client:
+         resp = await client.get(f"{BASE}/users/{uid}")
+         resp.raise_for_status()
+         return resp.json()
```

只有同步库可用时：

```python
result = await asyncio.to_thread(sync_lib.call, arg1, arg2)
```

**顺手加固**：把上面那个 `heartbeat` 放进开发环境的启动流程，堵超过 50ms 就打日志。它能抓出你根本想不到的阻塞点。

### 坑 2：`await` 忘了写，逻辑静默不执行

**现象**

日志里一条 `RuntimeWarning: coroutine 'save_ticket' was never awaited`，业务看起来"正常"，但工单一条没落库。

**定位方法**

```powershell
uv run python -W error::RuntimeWarning scripts/your_script.py
```

把警告升级成错误，问题立刻变成崩溃，堆栈直接指到漏 `await` 的那一行。

**根因**

`save_ticket(ticket)` 只是**创建了协程对象**，没有提交给事件循环。Python 在对象被垃圾回收时提醒你一句，然后**什么也没做**。

**修法**

```diff
- save_ticket(ticket)
+ await save_ticket(ticket)
```

如果是"发出去就不管"的场景，用 `asyncio.create_task(...)` 并**保留引用**：

```python
background: set[asyncio.Task] = set()
task = asyncio.create_task(save_ticket(ticket))
background.add(task)                      # 不保留引用可能被 GC 提前回收
task.add_done_callback(background.discard)
```

**顺手加固**：CI 里加 `-W error::RuntimeWarning`，这一整类 bug 直接绝迹。

### 坑 3：`gather` 把异常吞了，只报一个

**现象**

三个并发调用，两个失败，日志里只有一个错误；另一个失败的原因永远查不到。

**定位方法**

```python
results = await asyncio.gather(a(), b(), c(), return_exceptions=True)
for r in results:
    if isinstance(r, Exception):
        print("失败:", repr(r))
```

**根因**

`gather` 默认行为是"第一个异常向上抛，其余任务继续跑，它们的结果和异常都被丢弃"。如果你没有 `await` 到底或没有收集，就丢了。

**修法**

```diff
- results = await asyncio.gather(a(), b(), c())
+ async with asyncio.TaskGroup() as tg:
+     t1, t2, t3 = (tg.create_task(f()) for f in (a, b, c))
+ results = [t1.result(), t2.result(), t3.result()]
```

`TaskGroup` 在失败时会把**所有**异常打包成 `ExceptionGroup`，用 `except*` 逐个看，一个都不会丢。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么 I/O 并发能快约 3 倍，CPU 并发一点不快？</b></summary>

I/O 的时间花在"等"上，`await` 让出控制权，三个等待可以重叠，总耗时 ≈ 最慢那一个。CPU 计算期间没有 `await`，事件循环无法切换，只能一个算完再算下一个；即使用线程，GIL 也让纯 Python 计算串行。**异步加速的是"等"，不是"算"。**

</details>

<details>
<summary><b>Q2：`asyncio.gather` 和 `asyncio.TaskGroup` 该选哪个？</b></summary>

新代码默认 `TaskGroup`。它在任一任务失败时**自动取消兄弟任务**，并把所有异常打包成 `ExceptionGroup`；`gather` 默认只抛第一个、其余丢弃。除非你明确要"互不影响、各自跑完"，否则 `TaskGroup` 更安全。

</details>

<details>
<summary><b>Q3：为什么取消任务时 `finally` 必须写？</b></summary>

取消会抛 `CancelledError`，但它**仍然走 `finally`**。没有 `finally`，被取消请求占用的连接、文件句柄、流式响应都不会释放，长跑服务会慢慢泄漏到 OOM。

</details>

### 9.2 动手任务：写一个"带独立超时的批量请求"

**要求**

实现 `fetch_all(labels: list[str], timeout_s: float)`：

1. 每个请求**独立**超时（不是整体超时）。
2. 一个失败**不影响**其他请求成功。
3. 返回 `(ok: list[str], failed: list[tuple[str, str]])`，失败项带原因。
4. 用 `TaskGroup`，把超时异常也纳入聚合。

**验收标准**

- 3 个请求分别耗时 `0.1s / 2.0s / 0.2s`，`timeout_s=0.5`：返回 2 成功 1 失败，总耗时 ≈ `0.5s`（不是 `2.3s`）。
- 把 `timeout_s` 调到 `3.0`：3 个全成功，总耗时 ≈ `2.0s`。
- 失败项的原因里能看到 `TimeoutError` 字样。
- 补一个 pytest 用例覆盖"1 个超时"的场景。

**提示**：`asyncio.timeout()` 可以嵌套使用，包在**每个**任务内部才是独立超时。整体超时只需包在 `TaskGroup` 外层。

### 9.3 拓展挑战

给 `fetch_all` 加一个并发上限 `max_concurrency`，用 `asyncio.Semaphore` 实现。

**思考**：10 个请求、`max_concurrency=2` 时，总耗时大约是多少？为什么"限制并发"有时反而更快（提示：想想对方服务的限流和你的连接池）？

---

## 10. 本节小结与延伸

**三点回顾**

1. **并发不是"同时做很多事"，而是"在等的时候不闲着"**。`await` 是唯一的切换点，出现在哪里，并发就发生在哪里。
2. **I/O 型用异步（快约 3 倍），CPU 型要用多进程**（本节实测并发毫无收益）。I/O 并发 2.96 倍、CPU 并发 1.554s vs 1.547s，这两个数字请记住。
3. **所有外部调用都要有超时，所有取消都要有 `finally`，所有并发都要用 `TaskGroup`**。这三条是生产级与玩具的分界线。

**延伸阅读**

- Python 官方 `asyncio` 文档（协程、任务、事件循环）：https://docs.python.org/3/library/asyncio.html
- `asyncio.TaskGroup`（3.11+ 的结构化并发）：https://docs.python.org/3/library/asyncio-task.html#task-groups
- `asyncio.timeout`：https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout

---

## 修订记录

- 2026-11-17 初稿：五个实验全部实机运行，§7 输出为真实捕获；§6 两个"改坏"实验均实测（`0.368s` 假加速、`1.501s` 阻塞失效）；补 §5.5 取消时 `finally` 的时序、§8 坑 1 的心跳检测法
