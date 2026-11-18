# file: scripts/composite_demo.py
"""3.7 的场景：子图拆分 + 端到端（M3 验收）。

用法：
    uv run python -m scripts.composite_demo
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from servicedesk.graph.checkpoint import open_sqlite_checkpointer
from servicedesk.graph.composite import (
    build_approval_subgraph,
    build_composite_graph,
    build_composite_via_callable,
    build_retrieval_subgraph,
    describe_nested,
    pending_interrupt,
    resume_flow,
    run_flow,
)
from servicedesk.logging_setup import configure_logging


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


async def demo_subgraphs() -> None:
    header("第 1 步：两个可以单独跑的子图")
    retrieval = build_retrieval_subgraph()
    approval = build_approval_subgraph()

    print("  子图 A（检索）单独跑：")
    out = await retrieval.ainvoke({"title": "VPN 连不上", "description": "认证失败", "trace": []})
    print(f"    intent={out['intent']}  draft={out['draft'][:40]}…")
    print(f"    trace={out['trace']}  stages={out['stages']}")

    print("\n  子图 B（审批）没有草稿时会短路（不挂起）：")
    out2 = await approval.ainvoke({"ticket_id": "T-1", "draft": "", "trace": []})
    print(f"    decision={out2['decision']}  final={out2['final']}")
    print("    >>> 子图能被单独测试，这是拆分最大的收益。")


async def demo_composite() -> None:
    header("第 2 步：父图 = 检索子图 → 审批子图")
    graph = build_composite_graph()
    info = describe_nested(graph)

    print(f"  父图节点: {info['parent_nodes']}")
    print(f"  父图边  : {info['parent_edges']}")
    nested = info.get("nested_nodes") or []
    print(f"  展开后节点数: {len(nested)}  {nested[:8]}")
    print("  >>> 父图只有 2 个节点，但展开后能看到子图内部的 classify/fetch/…")


async def demo_end_to_end() -> None:
    header("第 3 步：端到端 —— 识别 → 检索 → 草稿 → 挂起 → 恢复 → 交付")

    tmp = Path(tempfile.mkdtemp(prefix="composite-"))
    async with open_sqlite_checkpointer(tmp / "flow.sqlite") as saver:
        graph = build_composite_graph(saver)

        print("  ① 发起：")
        await run_flow(
            graph, "t-flow", ticket_id="T-1001",
            title="VPN 连不上", description="客户端提示认证失败",
        )
        info = await pending_interrupt(graph, "t-flow")
        print(f"     挂起了吗: {info is not None}")
        print(f"     next    : {info['next']}   ← 注意是父图的 approval 节点")
        print(f"     审批单  : ticket={info['payload']['ticket_id']} risk={info['payload']['risk']}")
        print(f"     草稿    : {info['payload']['draft'][:44]}…")

        print("\n  ② 人类批准后恢复：")
        out = await resume_flow(graph, "t-flow", "approve")
        print(f"     decision={out['decision']}")
        print(f"     final   : {out['final'][:50]}…")
        print(f"     stages  : {out['stages']}   ← 走过的子图")
        print(f"     trace   : {out['trace']}")
        repeats = len(out["stages"]) - len(set(out["stages"]))
        if repeats:
            print()
            print(f"  ⚠️ 注意：retrieval 被跑了 {out['stages'].count('retrieval')} 次。")
            print("     这不是 demo 写错，是本版本 LangGraph 的行为：")
            print("     **子图内部 interrupt 后恢复，父图会重跑子图之前已完成的兄弟节点**。")
            print("     durability=async/sync/exit 三种都试过，都重跑。")
            print("     规避：① 让前置节点纯读、可重跑；② 把副作用放到审批之后；")
            print("           ③ 或者干脆别嵌子图——把 interrupt 放在父图（3.5 就是平的，没有重跑）。")

        header("第 4 步：资料的四种结局（同一张图，不同输入）")
        cases = [
            ("approve", "VPN 认证失败", "密码过期", "approve"),
            ("reject", "邮件附件太大", "50MB 发不出", "reject"),
            ("no-docs", "打印机坏了", "三楼不出纸", None),
        ]
        for thread, title, desc, decision in cases:
            await run_flow(graph, thread, ticket_id=f"T-{thread}", title=title, description=desc)
            info = await pending_interrupt(graph, thread)
            if info is None:
                state = await graph.aget_state({"configurable": {"thread_id": thread}})
                print(f"  {thread:<10} 未挂起 → final: {state.values.get('final')}")
                continue
            out = await resume_flow(graph, thread, decision)
            print(f"  {thread:<10} {decision:<8} → {out['final'][:34]}")


async def demo_callable() -> None:
    header("第 5 步：另一种接法 —— 用包装函数调子图")
    from langgraph.checkpoint.memory import InMemorySaver

    graph = build_composite_via_callable(InMemorySaver())

    await run_flow(graph, "t-call", ticket_id="T-2001", title="VPN 认证失败", description="需要重置")
    info = await pending_interrupt(graph, "t-call")
    print(f"  挂起了吗: {info is not None}（interrupt 仍然能冒泡）")
    out = await resume_flow(graph, "t-call", "approve")
    print(f"  final   : {out['final'][:44]}…")
    print(f"  trace   : {out['trace']}")
    print()
    print("  >>> 对比第 3 步的 trace：直接当节点时，子图内部的 trace 全保留；")
    print("      包装函数里我用一行 ['retrieval(callable)'] 替换了它 —— **信息量少了**。")
    print("      这就是「省事」与「可观测」的取舍。")
    print()
    print("  >>> 注意审批子图**不能**用包装函数：里面有 interrupt，")
    print("      包装函数会另起一次执行，父图的 checkpointer 上下文传不进去。")


def compare() -> None:
    header("第 6 步：两种接法怎么选")
    rows = [
        ("state 透传", "自动（要求同 schema）", "手写映射"),
        ("子图 interrupt", "自动冒泡", "需要显式处理"),
        ("内部 trace", "完整保留", "被包装层覆盖"),
        ("复用性", "高（图本身复用）", "中（包装函数要重写）"),
        ("适用", "同 schema、纯组合", "字段不同、要加前后逻辑"),
    ]
    print(f"  {'维度':<14}{'① 子图当节点':<26}{'② 包装函数'}")
    print("  " + "-" * 74)
    for name, a, b in rows:
        print(f"  {name:<14}{a:<26}{b}")


def m3_acceptance() -> None:
    header("第 7 步：M3 验收表")
    rows = [
        ("3.1 状态图替代 if-else", "CompiledStateGraph 跑通三节点", "✅"),
        ("3.2 State 与 reducer", "messages/trace 累加，派生字段现算", "✅"),
        ("3.3 条件边与循环", "三路分支 + 重试 + 兜底 + 刹车", "✅"),
        ("3.4 断点续跑", "真杀进程（os._exit）后从 step2 继续", "✅"),
        ("3.5 人工审批", "挂起 → 进程退出 → 新进程恢复 → 交付", "✅"),
        ("3.6 流式与可观测", "15s 任务首条进度 0.00s", "✅"),
        ("3.7 子图拆分", "父图调两个子图，interrupt 冒泡", "✅"),
    ]
    print(f"  {'能力':<26}{'证据':<40}{'状态'}")
    print("  " + "-" * 76)
    for name, evidence, status in rows:
        print(f"  {name:<26}{evidence:<40}{status}")
    print()
    print("  M3 完成标志：一条工单流程能分叉、能重试、能断点续跑、能等人审批，")
    print("  并且用户全程看得到进度。")


async def main() -> int:
    configure_logging(level="ERROR")

    await demo_subgraphs()
    await demo_composite()
    await demo_end_to_end()
    await demo_callable()
    compare()
    m3_acceptance()

    header("小结")
    print("  1. 子图可以单独构建、单独测试——这是拆分最大的收益。")
    print("  2. 同 schema 时，编译好的子图直接当节点用；state 自动透传。")
    print("  3. 子图里的 interrupt 会冒泡到父图，next 指向父图那个子图节点。")
    print("  4. 需要 state 映射或前后逻辑时，用包装函数——但要接受 trace 变薄。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
