# file: scripts/edges_demo.py
"""3.3 的场景：三路分支、重试循环、错误兜底、Command 跳转、死循环刹车。

用法：
    uv run python -m scripts.edges_demo
"""

from __future__ import annotations

import asyncio

from servicedesk.graph.edges import (
    build_branching_graph,
    build_command_graph,
    describe_routes,
)


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def show(label: str, out: dict) -> None:
    trace = " → ".join(out.get("trace") or [])
    print(f"  {label}")
    print(f"    走法    : {trace}")
    print(f"    尝试次数: {out.get('attempts', 0)}")
    print(f"    答复    : {(out.get('answer') or '')[:60]}")


async def demo_branches() -> None:
    header("第 1 步：三路分支")
    graph = build_branching_graph(empty_rounds=0)
    cases = [
        ("VPN 连不上", "客户端提示认证失败"),
        ("邮件发不出", "附件 50MB"),
        ("打印机坏了", "三楼不出纸"),
    ]
    for title, desc in cases:
        out = await graph.ainvoke(
            {"title": title, "description": desc, "max_attempts": 3, "trace": []}
        )
        show(f"「{title}」", out)
        print()


async def demo_retry() -> None:
    header("第 2 步：循环 —— 信息不足就回头再查")
    print("  场景：知识库第一次没返回结果（empty_rounds=1）")
    graph = build_branching_graph(empty_rounds=1, max_attempts=3)
    out = await graph.ainvoke(
        {"title": "VPN 认证失败", "description": "需要重置密码", "max_attempts": 3, "trace": []}
    )
    show("结果", out)
    print()
    print("  >>> 第一次 fetch 返回空 → 路由判定 retry → 回到同一个节点 → 第二次拿到资料。")
    print("      这就是「回头看」：图允许把边连回已经走过的节点。")


async def demo_brake() -> None:
    header("第 3 步：刹车 —— 重试到上限就放弃")
    print("  场景：知识库永远查不到（empty_rounds=99），max_attempts=3")
    graph = build_branching_graph(empty_rounds=99, max_attempts=3)
    out = await graph.ainvoke(
        {"title": "VPN 认证失败", "description": "查不到的问题", "max_attempts": 3, "trace": []}
    )
    show("结果", out)
    print()
    print("  >>> 第 3 次之后 route_after_fetch 返回 clarify，循环终止。")
    print("      没有这个刹车，图会一直转下去。")


async def demo_error_edge() -> None:
    header("第 4 步：错误边 —— 下游故障走兜底，而不是整图崩掉")
    print("  场景：描述里含「超时」→ fetch 写入 error")
    graph = build_branching_graph(empty_rounds=0)
    out = await graph.ainvoke(
        {"title": "VPN 认证失败", "description": "接口超时", "max_attempts": 3, "trace": []}
    )
    show("结果", out)
    print()
    print("  >>> 注意 trace 里是 fetch_vpn → fallback：错误没有抛出图外，")
    print("      而是被条件边导到了一个能给出人话的节点。")


async def demo_dead_loop() -> None:
    header("第 5 步：没有刹车会怎样 —— recursion_limit 兜住")
    print("  场景：empty_rounds 极大、max_attempts 极大，等于没有刹车")
    graph = build_branching_graph(empty_rounds=10**9, max_attempts=10**9)
    try:
        await graph.ainvoke(
            {"title": "VPN 认证失败", "description": "永远查不到", "max_attempts": 10**9, "trace": []},
            {"recursion_limit": 12},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  抛出: {type(exc).__name__}")
        print(f"  信息: {str(exc)[:110]}")
    print()
    print("  >>> LangGraph 有内置上限：实测默认是 10007 步（不是 25），超了抛 GraphRecursionError。")
    print("      但它只是最后一道保险——10007 步足够把你的账单和下游打爆。")
    print("      **业务上的 max_attempts 才是正确的刹车**：它能说出『为什么放弃』，递归上限只给异常。")


async def demo_command() -> None:
    header("第 6 步：Command —— 节点自己决定去哪")
    graph = build_command_graph(empty_rounds=1)
    out = await graph.ainvoke(
        {"title": "VPN 认证失败", "description": "需要重置密码", "max_attempts": 3, "trace": []}
    )
    show("结果", out)
    print()
    print("  >>> 逻辑和条件边版一样，但路由写在节点里（Command(goto=...)）。")
    print("      好处：判断与动作在同一个函数里，读起来是连贯的。")
    print("      代价：图的结构不再能从 add_conditional_edges 一眼看全。")


async def demo_structure() -> None:
    header("第 7 步：把结构导出来（含条件边的分支标签）")
    graph = build_branching_graph(empty_rounds=1)
    for source, target, label in describe_routes(graph):
        suffix = f"  [{label}]" if label else ""
        print(f"  {source:<14} → {target}{suffix}")


async def main() -> int:
    await demo_branches()
    await demo_retry()
    await demo_brake()
    await demo_error_edge()
    await demo_dead_loop()
    await demo_command()
    await demo_structure()

    header("小结")
    print("  1. add_conditional_edges：按 State 选下一站，分支与重试都用它。")
    print("  2. 循环 = 把边连回自己；**必须配 max_attempts**，否则靠递归上限兜。")
    print("  3. 错误边没有魔法：节点内 try/except 写 error，条件边导到兜底节点。")
    print("  4. Command(goto=...) 把路由写进节点，适合「判断和动作强耦合」的场景。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
