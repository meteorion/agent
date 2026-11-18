# file: scripts/langgraph_intro_demo.py
"""3.1 的场景：先让手写 if-else 崩三次，再上状态图。

用法：
    uv run python -m scripts.langgraph_intro_demo
"""

from __future__ import annotations

import asyncio
from typing import Any

from servicedesk.graph.minimal import build_minimal_graph, describe_graph

# ------------------------- 手写版：一长串 if-else -------------------------

STEPS = {"count": 0}


def _classify(title: str, description: str) -> str:
    STEPS["count"] += 1
    text = f"{title} {description}".lower()
    if any(word in text for word in ("vpn", "认证", "隧道")):
        return "vpn"
    if any(word in text for word in ("邮箱", "邮件", "附件")):
        return "email"
    return "other"


def _retrieve(intent: str) -> list[str]:
    STEPS["count"] += 1
    docs = {
        "vpn": [
            "认证失败通常有三类原因：密码过期、动态口令偏差、账号锁定。",
            "处理顺序：先在自助门户重置密码，再校准手机时间。",
        ],
        "email": ["单封邮件附件上限 25 MB。"],
        "other": [],
    }
    return docs.get(intent, [])


def _draft(docs: list[str], intent: str, *, fail: bool = False) -> dict[str, Any]:
    STEPS["count"] += 1
    if fail:
        raise RuntimeError("下游服务超时")          # 模拟第 3 步崩掉
    if not docs:
        draft = "资料中没有相关信息。"
    else:
        draft = "根据资料：" + " ".join(docs) + "[C1]"
    return {"draft": draft, "needs_approval": intent == "vpn" and "重置" in draft}


def handle_v0(title: str, description: str, *, approve: bool = False, fail: bool = False) -> dict:
    """手写编排：控制流 = if-else + 函数参数。

    注意它的"状态"是：title / description / intent / docs / draft / needs_approval
    —— 全都靠参数与返回值一手手传。
    """
    intent = _classify(title, description)
    docs = _retrieve(intent)

    if not docs:
        # 早期返回：**忘了带上 intent**，调用方只能看到一份残缺状态
        return {"status": "no_docs", "draft": "资料中没有相关信息。"}

    result = _draft(docs, intent, fail=fail)

    if result["needs_approval"] and not approve:
        # 审批只能"返回 pending"，中间过程全部丢掉（没有任何地方存）
        return {"status": "pending_approval", "draft": result["draft"]}

    return {"status": "ok", **result}


# ------------------------------- 演示 -------------------------------


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def demo_break_1() -> None:
    header("崩法 1：状态散落 —— 加一个字段要改多少处？")
    print("  手写版的字段出现在这些地方：")
    print("    _classify(title, description)            ← 2 个")
    print("    _retrieve(intent)                        ← 1 个")
    print("    _draft(docs, intent, fail)               ← 2 个")
    print("    handle_v0 的 4 个 return                 ← 每条都要手工对齐")
    print()
    print("  实测一次『早期返回忘了带字段』：")
    lost = handle_v0("打印机坏了", "没有相关资料覆盖的问题")
    print(f"    返回: {lost}")
    print(f"    keys: {sorted(lost)}")
    print("    → 没有 intent、没有 needs_approval。调用方拿到的是**残缺状态**，")
    print("      而它完全合法（没报错），所以 bug 会一路流到下游。")


def demo_break_2() -> None:
    header("崩法 2：无法断点 —— 崩一次，全部重来")
    STEPS["count"] = 0
    try:
        handle_v0("VPN 连不上", "认证失败", fail=True)
    except RuntimeError as exc:
        print(f"  第一次运行：在第 3 步崩溃（{exc}）")
    first = STEPS["count"]
    print(f"    已执行步骤数: {first}")

    STEPS["count"] = 0
    result = handle_v0("VPN 连不上", "认证失败")
    second = STEPS["count"]
    print(f"  重跑：{result['status']}，又执行了 {second} 步")
    print(f"    → 为了完成 3 个步骤的工作，总共执行了 {first + second} 次")
    print("      因为中间结果没有任何地方存下来。")


def demo_break_3() -> None:
    header("崩法 3：审批无处安放 —— 只能返回 pending，然后丢光上下文")
    pending = handle_v0("VPN 认证失败", "需要重置密码", approve=False)
    print(f"  未审批时: {pending}")
    print(f"    keys: {sorted(pending)}")
    print("    → 想让用户稍后点『批准』再继续，你得自己实现：")
    print("      ① 把中间状态存进数据库 ② 建一个 resume 接口 ③ 重启后能找到断点")
    print("      —— 这三件事手写编排里**一点都没准备**。")


async def demo_graph() -> None:
    header("换成状态图：State + Node + Edge")
    graph = build_minimal_graph()
    info = describe_graph(graph)

    print(f"  编译产物类型: {type(graph).__name__}")
    print(f"  节点: {info['nodes']}")
    print(f"  边  : {info['edges']}")
    print(f"  入口: {info['entry_edges']}")
    print()
    print("  Mermaid（粘到任何支持 mermaid 的地方都能渲染）：")
    for line in info["mermaid"].splitlines():
        print("    " + line)

    cases = [
        ("VPN 连不上", "客户端提示认证失败"),
        ("邮件附件太大", "附件 50MB 发不出去"),
        ("打印机坏了", "三楼打印机不出纸"),
    ]
    print()
    for title, description in cases:
        out = await graph.ainvoke({"title": title, "description": description, "trace": []})
        print(f"  输入: {title} / {description}")
        print(f"    意图: {out['intent']}   审批: {out['needs_approval']}")
        print(f"    走法: {' → '.join(out['trace'])}")
        print(f"    草稿: {out['draft'][:60]}")


def _dwidth(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _dwidth(text))


def compare() -> None:
    header("对比")
    rows = [
        ("状态在哪", "函数参数 + 返回值（散落）", "一个 State（集中）"),
        ("加字段", "改 N 个签名与 return", "只改 State 定义"),
        ("崩了怎么办", "从头重跑", "可断点恢复（3.4 Checkpointer）"),
        ("人工审批", "返回 pending，上下文丢失", "interrupt 挂起，状态自动落盘（3.5）"),
        ("可视化", "读代码", "get_graph() / mermaid"),
        ("可测试性", "只能整条链路测", "每个 Node 单独测"),
    ]
    width_a, width_b = 12, 30
    print("  " + _pad("维度", width_a) + _pad("手写 if-else", width_b) + "状态图")
    print("  " + "-" * (width_a + width_b + 40))
    for name, old, new in rows:
        print("  " + _pad(name, width_a) + _pad(old, width_b) + new)


async def main() -> int:
    demo_break_1()
    demo_break_2()
    demo_break_3()
    await demo_graph()
    compare()

    header("小结")
    print("  1. 手写编排的三个崩法：状态散落、无法断点、审批无处安放。")
    print("  2. 状态图把控制流变成数据：State 集中、Node 纯函数、Edge 显式。")
    print("  3. compile() 得到的是可执行图（CompiledStateGraph），不是代码生成器。")
    print("  4. 这一节还是直线；分支在 3.3，断点在 3.4，审批在 3.5。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
