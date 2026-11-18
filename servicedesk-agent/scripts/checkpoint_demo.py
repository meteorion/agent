# file: scripts/checkpoint_demo.py
"""3.4 的场景：内存恢复、真实杀进程恢复、时间旅行。

用法：
    uv run python -m scripts.checkpoint_demo
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from servicedesk.graph.checkpoint import (
    TOTAL_STEPS,
    build_workflow_graph,
    ensure_defaults,
    history,
    make_memory_checkpointer,
    open_sqlite_checkpointer,
    replay,
    run,
    snapshot,
    thread_config,
)


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


async def demo_memory() -> None:
    header("第 1 步：内存 Checkpointer —— 同进程内恢复（异常版）")

    saver = make_memory_checkpointer()
    graph = build_workflow_graph(saver)

    # 造一个"第 3 步一定失败"的图：用异常模拟业务错误（进程还活着）
    async def failing_step3(state):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.01)
        raise RuntimeError("第 3 步调用的下游接口 500")

    from langgraph.graph import END, START, StateGraph

    from servicedesk.graph.checkpoint import WorkState, finish, make_step

    builder = StateGraph(WorkState)
    builder.add_node("step1", make_step(1))
    builder.add_node("step2", make_step(2))
    builder.add_node("step3", failing_step3)
    builder.add_node("step4", make_step(4))
    builder.add_node("finish", finish)
    builder.add_edge(START, "step1")
    builder.add_edge("step1", "step2")
    builder.add_edge("step2", "step3")
    builder.add_edge("step3", "step4")
    builder.add_edge("step4", "finish")
    builder.add_edge("finish", END)
    fragile = builder.compile(checkpointer=saver)

    thread = "t-memory"
    try:
        await run(fragile, thread, {"ticket_id": "T-2001", "trace": []})
    except RuntimeError as exc:
        print(f"  第一次运行失败: {exc}")

    snap = await snapshot(fragile, thread)
    print(f"  失败后停在哪: step={snap['step']} trace={snap['trace']} next={snap['next']}")
    print("  >>> 注意 next：下一步是 step3 —— 说明前两步的成果被保住了。")

    # 换成不会失败的图，用同一个 thread_id 继续（checkpointer 是同一个对象）
    healed = build_workflow_graph(saver)
    out = await run(healed, thread)
    print(f"  恢复后跑完: step={out.get('step')} trace={out.get('trace')}")
    print(f"  答复: {out.get('answer')}")


async def demo_kill() -> None:
    header("第 2 步：真的杀进程 —— os._exit(1)，无清理、无 finally")

    tmpdir = Path(tempfile.mkdtemp(prefix="ckpt-"))
    db_path = tmpdir / "checkpoints.sqlite"
    thread = "t-killed"

    print(f"  数据库: {db_path}")
    print(f"  子进程环境: CKPT_CRASH_AT=3（跑到第 3 步直接 os._exit）")

    side_effect_file = tmpdir / "side-effects.log"
    env = {
        **os.environ,
        "CKPT_CRASH_AT": "3",
        "CKPT_SIDE_EFFECT_FILE": str(side_effect_file),
        "PYTHONIOENCODING": "utf-8",
    }
    first = subprocess.run(
        [sys.executable, "-m", "scripts.checkpoint_worker", str(db_path), thread],
        env=env,
        capture_output=True,
        text=True,
    )
    print(f"  第一次退出码: {first.returncode}（非 0 表示被中断）")
    if first.stdout.strip():
        print(f"    子进程输出: {first.stdout.strip()[:100]}")

    # 重新打开同一个数据库（模拟服务重启）
    async with open_sqlite_checkpointer(db_path) as saver:
        graph = build_workflow_graph(saver)
        snap = await snapshot(graph, thread)
        print(f"  重启后读到的断点: step={snap['step']} trace={snap['trace']} next={snap['next']}")

        # 续跑进程也要开副作用计数，否则看不出"重跑"这件事
        os.environ["CKPT_SIDE_EFFECT_FILE"] = str(side_effect_file)
        try:
            out = await run(graph, thread)
        finally:
            os.environ.pop("CKPT_SIDE_EFFECT_FILE", None)
        print(f"  续跑完成: step={out.get('step')} trace={out.get('trace')}")
        print(f"  答复: {out.get('answer')}")

        print(f"\n  >>> 关键点：trace 是 step1,step2,step3,step4 —— 没有从头重来。")
        print(f"      被中断的 step3 重新执行了一次，前两步的成果来自磁盘。")

        if side_effect_file.exists():
            lines = side_effect_file.read_text(encoding="utf-8").split()
            counts = {name: lines.count(name) for name in sorted(set(lines))}
            print(f"\n  副作用文件（每步追加一行）: {counts}")
            print(f"  >>> step3 出现了 {counts.get('step3', 0)} 次！")
            print(f"      **被中断的节点会重跑，副作用就会重复发生**——")
            print(f"      所以发通知/扣款这类操作必须幂等（或用 1.5 的幂等键）。")

        header("第 3 步：时间旅行 —— 回看每一步的快照")
        rows = await history(graph, thread)
        print(f"  共 {len(rows)} 个快照（新的在前）：")
        for row in rows[:6]:
            print(f"    step={row['step']:<2} next={row['next']} trace={row['trace']}")

        header("第 4 步：从历史快照重放")
        # 找一个"停在 step2 之后"的快照
        target = next((row for row in rows if row["step"] == 2), None)
        if target and target["checkpoint_id"]:
            print(f"  选中 checkpoint={target['checkpoint_id'][:20]}… （step=2）")
            replayed = await replay(graph, thread, target["checkpoint_id"])
            print(f"  重放结果: step={replayed.get('step')} trace={replayed.get('trace')}")
            print("  >>> 这会从 step2 之后重新往下跑，并在同一 thread 上形成新分支。")
        else:
            print("  没找到 step=2 的快照")

    header("第 5 步：Schema 演进 —— 老 checkpointer 里没有新字段")
    old_state = {"ticket_id": "T-3001", "trace": ["step1"]}
    print(f"  老快照: {old_state}")
    print(f"  补默认后: {ensure_defaults(old_state)}")
    print("  >>> State 用 total=False，缺字段不会反序列化失败；")
    print("      但下游可能假设字段存在，所以恢复时统一补一遍默认值。")


async def demo_compare() -> None:
    header("第 6 步：三种 Checkpointer 怎么选")
    rows = [
        ("内存 InMemorySaver", "✅ 零配置\n✅ 最快", "❌ 进程一停就没了", "测试 / demo"),
        ("SQLite SqliteSaver", "✅ 单文件落盘\n✅ 零服务依赖", "⚠️ 并发写有锁竞争", "单机 / 小规模"),
        ("Postgres PostgresSaver", "✅ 并发安全\n✅ 多实例共享", "⚠️ 要运维数据库", "生产 / 多实例"),
    ]
    print(f"  {'类型':<24}{'优点':<22}{'代价':<22}{'适用'}")
    print("  " + "-" * 84)
    for kind, good, bad, use in rows:
        first = True
        for good_line, bad_line in zip(good.split("\n"), bad.split("\n")):
            prefix = kind if first else ""
            print(f"  {prefix:<24}{good_line:<22}{bad_line:<22}{use if first else ''}")
            first = False
    print()
    print("  >>> 三种的**调用方式完全一样**，只换构造。本机实测了内存与 SQLite；")
    print("      Postgres 版需要真实数据库，本节未运行（代码路径见 checkpoint.py 注释）。")


async def main() -> int:
    print(f"总步数: {TOTAL_STEPS}")
    print(f"thread_config 示例: {thread_config('demo-thread')}")

    await demo_memory()
    await demo_kill()
    await demo_compare()

    header("小结")
    print("  1. thread_id 决定「哪段历史」；换 id 就是另起一段。")
    print("  2. 内存版只在进程内有效；要抗崩溃必须落盘（SQLite/Postgres）。")
    print("  3. 被中断的节点会**重新执行**——所以副作用必须幂等（1.5 坑 2）。")
    print("  4. get_state_history 能回看每一步，也能从旧快照重放（形成新分支）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
