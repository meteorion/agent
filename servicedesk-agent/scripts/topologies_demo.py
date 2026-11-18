# file: scripts/topologies_demo.py
"""4.3 的场景：supervisor（中心调度） vs handoff（对等转移）。

用法：
    uv run python -m scripts.topologies_demo
"""

from __future__ import annotations

import asyncio

from servicedesk.logging_setup import configure_logging
from servicedesk.multiagent.topologies import (
    TASKS,
    build_handoff_graph,
    build_supervisor_graph,
    compare_topologies,
)


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _dwidth(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _dwidth(text))


def demo_structures() -> None:
    header("第 1 步：两种拓扑长什么样")

    sup = build_supervisor_graph()
    ho = build_handoff_graph()

    def nodes(graph):
        return [n for n in graph.get_graph().nodes if not n.startswith("__")]

    def edges(graph):
        return [(e.source, e.target) for e in graph.get_graph().edges
                if not e.source.startswith("__") and not e.target.startswith("__")]

    print(f"  supervisor 节点: {nodes(sup)}")
    print(f"  supervisor 边  : {edges(sup)}")
    print()
    print(f"  handoff    节点: {nodes(ho)}")
    print(f"  handoff    边  : {edges(ho)}")
    print()
    print("  >>> 注意 handoff 的边表里**看不到** write→review、review→write：")
    print("      它们由 Command(goto=...) 在运行时决定。这是它的主要缺点。")


async def demo_compare() -> None:
    header(f"第 2 步：同一批 {len(TASKS)} 个任务，两种拓扑")
    print("  任务：")
    for ticket_id, title, description, keyword in TASKS:
        print(f"    {ticket_id}  {title:<12} 期望含「{keyword}」")

    data = await compare_topologies()
    sup, ho = data["supervisor"], data["handoff"]

    print()
    print(f"  {pad('指标', 22)}{pad('supervisor', 20)}{'handoff'}")
    print("  " + "-" * 60)
    rows = [
        ("成功", f"{sup['success']}/{sup['tasks']}", f"{ho['success']}/{ho['tasks']}"),
        ("每任务轮次", str(sup["turns_per_task"]), str(ho["turns_per_task"])),
        ("每任务交接说明", str(sup["notes_per_task"]), str(ho["notes_per_task"])),
        ("每任务 trace 长度", str(sup["trace_len_per_task"]), str(ho["trace_len_per_task"])),
    ]
    for label, a, b in rows:
        print(f"  {pad(label, 22)}{pad(a, 20)}{b}")

    if ho["turns_per_task"]:
        ratio = sup["turns_per_task"] / ho["turns_per_task"]
        print()
        print(f"  轮次比: supervisor 是 handoff 的 {ratio:.1f} 倍")
        print("  >>> 因为 supervisor 每派一次活、worker 干完都要**回中心**再走下一跳。")
        print("      5 个 worker 就多出 5 次往返——这就是'看得见全局'的票价。")


async def demo_traces() -> None:
    header("第 3 步：逐任务看 trace —— 差在哪一跳")
    sup = build_supervisor_graph()
    ho = build_handoff_graph()

    ticket_id, title, description, _ = TASKS[0]
    payload = {"ticket_id": ticket_id, "title": title, "description": description,
               "trace": [], "notes": []}

    sup_out = await sup.ainvoke(payload, {"recursion_limit": 40})
    ho_out = await ho.ainvoke(payload, {"recursion_limit": 40})

    print(f"  任务: {ticket_id} {title}")
    print()
    print(f"  supervisor trace ({len(sup_out['trace'])} 条):")
    print(f"    {' → '.join(sup_out['trace'])}")
    print()
    print(f"  handoff trace ({len(ho_out['trace'])} 条):")
    print(f"    {' → '.join(ho_out['trace'])}")
    print()
    print("  >>> supervisor 的 trace 里有一半是 `supervisor->x`：")
    print("      **这些跳不干业务，只做调度**。handoff 里没有它们。")


async def demo_pingpong() -> None:
    header("第 4 步：handoff 的阴暗面 —— 踢皮球与止损")
    ho = build_handoff_graph()

    ticket_id, title, description, _ = TASKS[2]      # "打印机坏了" —— 没有资料
    out = await ho.ainvoke(
        {"ticket_id": ticket_id, "title": title, "description": description,
         "trace": [], "notes": []},
        {"recursion_limit": 40},
    )

    print(f"  任务: {ticket_id} {title}（知识库里没有这类资料）")
    print(f"  trace: {' → '.join(out['trace'])}")
    print(f"  visits: {out['visits']}")
    print(f"  最终: {out['final']}")
    print()
    print("  >>> review 打回 write，write 又交回 review —— 来回两轮后")
    print("      writer 自己检测到'我被访问 3 次了'，主动止损。")
    print()
    print("  **handoff 拓扑必须自己管这件事**：没有中心节点帮你发现循环。")
    print("  本节的止损条件是 `visits['write'] > 2`；生产里更好的做法是")
    print("  在 Command 里带上'已经试过哪些方案'，避免重复同一条死路。")


def demo_tradeoff() -> None:
    header("第 5 步：取舍")
    rows = [
        ("轮次开销", "高（每次回中心）", "低（少一跳）"),
        ("全局可见性", "✅ 中心掌握全状态", "❌ 每个 agent 只知道自己那段"),
        ("加新角色", "改中心一处", "改前后两个 agent 的 goto"),
        ("图的可读性", "✅ 边表完整", "❌ 跳转藏在 Command 里"),
        ("循环风险", "低（中心能计数）", "高（要靠 visits 自救）"),
        ("适合", "角色多、需要统一策略", "链路短、角色对等"),
    ]
    print(f"  {pad('维度', 14)}{pad('supervisor', 26)}{'handoff'}")
    print("  " + "-" * 70)
    for name, a, b in rows:
        print(f"  {pad(name, 14)}{pad(a, 26)}{b}")
    print()
    print("  >>> 判据：**角色 > 3 个、或需要统一策略/审计 → supervisor；")
    print("      链路 2–3 跳、角色对等 → handoff。**")


async def main() -> int:
    configure_logging(level="ERROR")

    demo_structures()
    await demo_compare()
    await demo_traces()
    await demo_pingpong()
    demo_tradeoff()

    header("小结")
    print("  1. supervisor：worker 回中心，轮次约翻倍，换来全局可见与循环可控。")
    print("  2. handoff：worker 直接交给下一个，少一跳，但边表看不见跳转。")
    print("  3. handoff 必须自带止损（visits 计数），否则踢皮球没人管。")
    print("  4. 角色多、要统一策略 → supervisor；链路短、角色对等 → handoff。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
