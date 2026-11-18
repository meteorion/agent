# file: scripts/approval_demo.py
"""3.5 的场景：审批挂起 → 进程退出 → 新进程恢复 → 结果正确。

用法：
    uv run python -m scripts.approval_demo
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from servicedesk.graph.approval import (
    build_approval_graph,
    decide,
    pending,
    start,
)
from servicedesk.graph.approval import open_approval_checkpointer as _open


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


async def demo_basic() -> None:
    header("第 1 步：发起审批 —— 图停在 human_gate")

    async with _open(":memory:") as saver:
        graph = build_approval_graph(saver)
        out = await start(
            graph, "t-1", ticket_id="T-1001", draft="重置密码的答复（涉及账号操作）", risk="high"
        )

        info = out["pending"]
        print(f"  挂起了吗: {info is not None}")
        print(f"  next    : {info['next']}   ← 停在 human_gate")
        print(f"  trace   : {info['trace']}  ← 只完成了 prepare")
        print()
        print("  交给人类的审批单（这就是前端要渲染的东西）：")
        for key, value in info["payload"].items():
            if key == "expires_at":
                print(f"    {key:<12} {value:.0f}（{time.strftime('%H:%M:%S', time.localtime(value))} 截止）")
            else:
                print(f"    {key:<12} {value}")


async def demo_decisions(tmp_path: Path) -> None:
    header("第 2 步：三种决定 —— 批准 / 拒绝 / 改写")

    async with _open(tmp_path / "decisions.sqlite") as saver:
        graph = build_approval_graph(saver)

        for index, (thread, decision, label) in enumerate(
            [
                ("t-approve", "approve", "批准"),
                ("t-reject", "reject", "拒绝"),
                ("t-edit", {"action": "edit", "text": "认证失败请先自行重置密码，无需人工。"}, "改写"),
            ]
        ):
            await start(graph, thread, ticket_id=f"T-{2000 + index}", draft="原始答复", risk="high")
            result = await decide(graph, thread, decision)
            print(f"  {label}: decision={result['decision']}")
            print(f"    最终: {result['final']}")
            print(f"    走法: {' → '.join(result['trace'])}")
            print()


async def demo_expired(tmp_path: Path) -> None:
    header("第 3 步：超时 —— 即使人点了批准，过期也算作废")

    async with _open(tmp_path / "expired.sqlite") as saver:
        graph = build_approval_graph(saver)
        # timeout_s=-1：一开始就已经过期，模拟"人都睡了三天才点批准"
        await start(graph, "t-expired", ticket_id="T-3001", draft="过期测试", timeout_s=-1)
        result = await decide(graph, "t-expired", "approve")

        print(f"  人点了: approve")
        print(f"  实际走: {' → '.join(result['trace'])}")
        print(f"  最终  : {result['final']}")
        print()
        print("  >>> interrupt 本身没有定时器：超时是**恢复时判断**出来的。")
        print("      真正的『到点自动作废』需要一个外部定时任务来投递 resume。")


async def demo_process_exit(tmp_path: Path) -> None:
    header("第 4 步：进程退出 —— 挂起期间状态只在 SQLite 里")
    db = tmp_path / "approval.sqlite"
    thread = "t-process"

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    child = subprocess.run(
        [sys.executable, "-m", "scripts.approval_worker", str(db), thread, "T-4001"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    print(f"  子进程退出码: {child.returncode}")
    if child.stdout.strip():
        print(f"    输出: {child.stdout.strip()[:120]}")
    print("  >>> 子进程已经结束，内存里什么都没有了。")

    # 全新进程：重新打开数据库，接着审批
    async with _open(db) as saver:
        graph = build_approval_graph(saver)
        info = await pending(graph, thread)
        print(f"\n  新进程读到待审批: {info is not None}")
        print(f"    审批单的 ticket: {info['payload']['ticket_id']}")
        print(f"    next           : {info['next']}")

        result = await decide(graph, thread, "approve")
        print(f"\n  审批后: decision={result['decision']}")
        print(f"    最终: {result['final']}")
        print(f"    走法: {' → '.join(result['trace'])}")


async def demo_gate_rerun() -> None:
    header("第 5 步：⚠️ 被 resume 的节点会从头重跑")
    calls = {"enter": 0, "side_effect": 0}

    from typing import TypedDict

    class Mini(TypedDict, total=False):
        decision: str

    async def gate(state):  # type: ignore[no-untyped-def]
        calls["enter"] += 1
        calls["side_effect"] += 1          # 模拟"发通知"
        print(f"    进入 gate 第 {calls['enter']} 次（副作用已执行 {calls['side_effect']} 次）")
        answer = interrupt({"ask": "批准？"})
        print(f"    interrupt 返回: {answer!r}")
        return {"decision": str(answer)}

    builder = StateGraph(Mini)
    builder.add_node("gate", gate)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", END)

    async with AsyncSqliteSaver.from_conn_string(":memory:") as saver:
        await saver.setup()
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "t-rerun"}}

        print("  第一次执行：")
        await graph.ainvoke({}, config=config)
        print("  人类批准：")
        await graph.ainvoke(Command(resume="approve"), config=config)

    print()
    print(f"  结果：gate 执行了 {calls['enter']} 次，副作用发生了 {calls['side_effect']} 次")
    print("  >>> 所以 interrupt() 之前**不能**放发通知/扣款这类操作。")
    print("      正确做法：副作用放在 interrupt 之后的独立节点里。")


async def main() -> int:
    tmp_path = Path(tempfile.mkdtemp(prefix="approval-"))

    await demo_basic()
    await demo_decisions(tmp_path)
    await demo_expired(tmp_path)
    await demo_process_exit(tmp_path)
    await demo_gate_rerun()

    header("小结")
    print("  1. interrupt(payload) 把图挂在当前节点，payload 就是给人类的审批单。")
    print("  2. 挂起期间状态在 checkpointer 里，进程可以退出（第 4 步实测）。")
    print("  3. Command(resume=...) 从挂起点继续；超时要在恢复时自己判断。")
    print("  4. **被 resume 的节点从头重跑**：副作用必须放在 interrupt 之后。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
