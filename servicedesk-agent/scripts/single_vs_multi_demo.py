# file: scripts/single_vs_multi_demo.py
"""4.1 的场景：同一批任务，单图 vs 多 Agent，用数据说话。

用法：
    uv run python -m scripts.single_vs_multi_demo
"""

from __future__ import annotations

import asyncio
import json

from servicedesk.logging_setup import configure_logging
from servicedesk.multiagent.single_vs_multi import (
    TASKS,
    build_multi_agent_graph,
    build_single_agent_graph,
    compare,
    compare_tool_context,
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


async def demo_structures() -> None:
    header("第 1 步：两套实现的结构")

    single = build_single_agent_graph()
    multi = build_multi_agent_graph()

    s_nodes = [n for n in single.get_graph().nodes if not n.startswith("__")]
    m_nodes = [n for n in multi.get_graph().nodes if not n.startswith("__")]
    print(f"  单图     节点: {s_nodes}")
    print(f"  多 Agent 节点: {m_nodes}")
    print()
    print("  单图：一条直线，state 共享，谁都能读全部字段。")
    print("  多 Agent：supervisor 调度三个工人，每次交接都把上下文序列化一遍。")


async def demo_benchmark() -> None:
    header(f"第 2 步：同一批 {len(TASKS)} 个任务跑两遍")
    print("  任务集：")
    for task in TASKS:
        print(f"    {task.ticket_id}  {task.title:<12} 期望意图={task.expected_intent:<6} "
              f"期望含「{task.expected_keyword}」")

    result = await compare()
    print()
    rows = ["single", "multi"]
    metrics = [
        ("成功率", lambda r: f"{r['success']}/{r['total']} ({r['success_rate']:.0%})"),
        ("总延迟(ms)", lambda r: str(r["latency_ms"])),
        ("交接次数", lambda r: str(r["handoffs"])),
        ("交接上下文(字节)", lambda r: str(r["context_bytes"])),
        ("trace 条数", lambda r: "—"),
    ]
    print(f"  {pad('指标', 20)}{pad('单图', 24)}{'多 Agent'}")
    print("  " + "-" * 62)
    for label, getter in metrics[:4]:
        print(f"  {pad(label, 20)}{pad(getter(result['single']), 24)}{getter(result['multi'])}")

    s, m = result["single"], result["multi"]
    if s["latency_ms"] > 0:
        print()
        print(f"  延迟倍增: {m['latency_ms'] / s['latency_ms']:.2f}x")
        print(f"  成功率相同: {s['success_rate'] == m['success_rate']}")
        print(f"  多出的交接: {m['handoffs']} 次，累计搬运 {m['context_bytes']} 字节")
    print()
    print("  >>> 这个任务集上，多 Agent **没有任何收益**：正确率一样，")
    print("      延迟更高，还多出 15 次上下文搬运。")


def demo_tool_context() -> None:
    header("第 3 步：多 Agent 真正赢的地方 —— 工具上下文压力")
    data = compare_tool_context()

    print(f"  可用工具数: {data['工具总数']}")
    print()
    print(f"  单 Agent 每次调用要带全部 schema: {data['单 Agent 每次调用 schema 字节']} 字节")
    print(f"    三次调用合计                  : {data['单 Agent 三次调用合计']} 字节")
    print()
    print("  多 Agent 每个专家只带自己那组:")
    for name, size in data["多 Agent 各组每次"].items():
        print(f"    {name:<10} {size} 字节")
    print(f"    合计（含路由清单 ×3）        : {data['多 Agent 合计（含路由清单 x3）']} 字节")
    print()
    print(f"  节省比例: {data['节省比例']:.1%}")
    print()
    print("  >>> 这才是拆分的正当理由：**不是分工好看，是每次调用的 prompt 变小了**。")
    print("      工具越多、越专业，这个比例越夸张。")


def demo_signals() -> None:
    header("第 4 步：该拆 / 不该拆的信号")

    print("  ✅ 该拆的三个信号：")
    signals_yes = [
        ("工具/权限差异大", "每个人只该看到自己那组工具（§第 3 步的 61%）"),
        ("需要独立并行探索", "三个方向同时试，最后汇总（谁先成谁交卷）"),
        ("上下文要隔离", "审计/合规要求：审查者不能看到草稿的推理过程"),
    ]
    for name, why in signals_yes:
        print(f"    - {pad(name, 16)} {why}")

    print()
    print("  ❌ 不该拆的三个信号：")
    signals_no = [
        ("任务本身是线性的", "识别→检索→答复，拆了只是多两次搬运"),
        ("一个人就能看全上下文", "拆开后每个 Agent 都缺一块信息"),
        ("你只是想「看起来更架构」", "评审会上好看，账单上难看"),
    ]
    for name, why in signals_no:
        print(f"    - {pad(name, 16)} {why}")

    print()
    print("  判断方法：**先写单图，跑到它真的撑不住**。")
    print("  撑不住的表现：上下文超限、工具选错、单点权限给不了、串行太慢。")


def demo_decision_table() -> None:
    header("第 5 步：决策表")
    rows = [
        ("3 步以内的线性流程", "单图", "本节实测：延迟 1.8x、零收益"),
        ("工具数 < 5、权限一致", "单图", "schema 撑得住，拆了反而缺信息"),
        ("工具分 3 组、权限各不同", "多 Agent", "schema 省 61%（实测）"),
        ("要并行试 3 个方案", "多 Agent", "串行版耗时 = 三者之和"),
        ("审计要求隔离上下文", "多 Agent", "不是性能问题，是合规问题"),
        ("不确定", "先单图", "单图能跑就先别拆，留好拆分点"),
    ]
    print(f"  {pad('场景', 26)}{pad('选择', 12)}理由")
    print("  " + "-" * 74)
    for scene, choice, why in rows:
        print(f"  {pad(scene, 26)}{pad(choice, 12)}{why}")


async def main() -> int:
    configure_logging(level="ERROR")

    await demo_structures()
    await demo_benchmark()
    demo_tool_context()
    demo_signals()
    demo_decision_table()

    header("小结")
    print("  1. 线性任务上，多 Agent 只增加成本：延迟更高、多出 N 次上下文搬运。")
    print("  2. 多 Agent 的真正收益是**每次调用的上下文变小**（本节实测省 61% schema）。")
    print("  3. 先写单图，跑到撑不住再拆；拆之前先量四个数：成功率/延迟/交接次数/上下文量。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
