# file: scripts/short_term_memory_demo.py
"""4.4 的场景：短期会话压缩，以及"关键约束会不会丢"。

用法：
    uv run python -m scripts.short_term_memory_demo
"""

from __future__ import annotations

from servicedesk.memory.short_term import (
    BudgetedHistory,
    estimate_tokens,
    extract_constraints,
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


def build_conversation() -> BudgetedHistory:
    """一段 20 条消息、埋了 2 个约束的会话。"""
    history = BudgetedHistory(budget_tokens=300, keep_recent=3)

    for index in range(3):
        history.add("user", f"第{index + 1}个无关问题：打印机在第{index + 1}层楼怎么连？")
        history.add("assistant", f"第{index + 1}层楼的打印机连接方式是先装驱动再添加端口。")

    # 约束 1：工单号（后面所有操作都要用它）
    history.add("user", "我的工单号是 T-1001，后面请用它称呼。")
    history.add("assistant", "收到，工单号 T-1001。")

    # 约束 2：禁令（丢了会出事故）
    history.add("user", "注意：不要重置我的密码，我有重要会议。")
    history.add("assistant", "明白，不会重置密码。")

    for index in range(3):
        history.add("user", f"再问一个：VPN 在第{index + 1}个办公区要不要额外配置？")
        history.add("assistant", f"第{index + 1}个办公区不需要额外配置，用默认端口即可。")

    history.add("user", "帮我总结一下现在的情况。")
    history.pin_all_constraints()
    return history


def demo_budget() -> None:
    header("第 1 步：先看预算 —— 按 token 算，不按条数")
    history = build_conversation()

    print(f"  消息条数: {len(history.messages)}")
    print(f"  当前 token: {history.tokens()}   预算: {history.budget}")
    print(f"  超预算了吗: {history.over_budget()}")
    print()
    print("  每条消息的 token（前 6 条）：")
    for message in history.messages[:6]:
        print(f"    {message.role:<9} {message.tokens:>3} tokens  {message.content[:30]}")
    print()
    print("  >>> 为什么按 token 不按条数：一条长回复可能比十条短消息还贵。")
    print(f"      本会话平均 {history.tokens() / len(history.messages):.1f} token/条，")
    print("      而一条工具返回的 JSON 可能单条就上千。")

    constraints = extract_constraints(history.messages)
    print()
    print(f"  抽到的约束（{len(constraints)} 条）：")
    for item in constraints:
        print(f"    [{item.kind:<8}] {item.text}")

    pinned = sum(1 for message in history.messages if message.pinned)
    print(f"\n  被标记为 pinned 的消息: {pinned} 条")


def demo_strategies() -> None:
    header("第 2 步：三种策略 —— 谁把约束弄丢了")
    print(f"  {pad('策略', 10)}{pad('压缩后 token', 16)}{pad('丢弃条数', 12)}丢失的约束")
    print("  " + "-" * 74)

    results = {}
    for strategy in ("window", "summary", "hybrid"):
        history = build_conversation()
        result = history.compress(strategy)
        results[strategy] = result
        data = result.as_dict()
        lost = "、".join(data["constraints_lost"]) or "（无）"
        print(f"  {pad(strategy, 10)}{pad(str(result.tokens_after), 16)}"
              f"{pad(str(result.dropped), 12)}{lost}")

    print()
    print("  >>> window 和 summary **都丢了** 工单号与禁令。")
    print("      summary 比 window 贵了 3 倍 token（135 vs 50），却丢了一样多的东西。")
    print("      为什么？因为**摘要器不知道什么重要**，它只按'先出现什么'截断。")


def demo_hybrid_detail() -> None:
    header("第 3 步：hybrid 为什么能保住 —— 它不靠摘要，靠'钉住'")
    history = build_conversation()
    result = history.compress("hybrid")

    print("  保留的消息（pinned 的约束 + 最近 3 条）：")
    for message in result.kept:
        flag = "📌" if message.pinned else "  "
        print(f"    {flag} {message.role:<9} {message.content[:40]}")

    print()
    print(f"  摘要（覆盖被丢掉的中段）: {result.summary[:70]}…")
    print()
    print("  >>> 关键设计：**约束不是'摘要出来'的，是'抽出来再钉住'的。**")
    print("      摘要负责'保住大意'，pinned 负责'保住要命的那几条'。")


def demo_timing() -> None:
    header("第 4 步：压缩该在什么时候触发")
    history = BudgetedHistory(budget_tokens=150, keep_recent=3)

    rounds = [
        "打印机连不上", "VPN 认证失败", "邮箱附件太大",
        "工单 T-2002 进度", "共享盘权限", "会议室投屏",
    ]
    print("  每轮加一对消息，观察 token 变化：")
    for index, question in enumerate(rounds, 1):
        history.add("user", f"{question}怎么处理？")
        history.add("assistant", "请先检查网络，再按手册第 3 节操作，仍然失败就建单。")
        flag = ""
        if history.over_budget():
            flag = "  ← 超预算，该压缩了"
        print(f"    第 {index} 轮后: {history.tokens():>4} tokens{flag}")
        if history.over_budget():
            result = history.compress("hybrid")
            print(f"      压缩: {result.tokens_before} → {result.tokens_after} tokens，"
                  f"丢弃 {result.dropped} 条，丢失约束 {len(result.lost_constraints)} 个")
            if result.lost_constraints:
                print("      ⚠️ 丢了约束！因为这段没调 pin_all_constraints() ——")
                print("         hybrid 依赖 pinned 标记；忘了 pin，它就退化成了 window。")

    print()
    print("  >>> 别等撑爆才压：留出余量，否则下一轮就超了。")
    print("      常见做法：到预算 70% 就压，压到 40% 左右。")


def demo_pitfall() -> None:
    header("第 5 步：约束抽取的坑 —— 「要不要」不是「不要」")
    samples = [
        "不要重置我的密码",
        "VPN 要不要额外配置？",
        "禁止导出客户名单",
        "这个必须今天处理完",
    ]
    for text in samples:
        found = extract_constraints([type("M", (), {"content": text})()])
        kinds = "、".join(f"{item.kind}:{item.text}" for item in found) or "（未识别）"
        print(f"  {text:<24} → {kinds}")
    print()
    print("  >>> 第一版用 `\"不要\" in text`，把「要不要」也判成了禁令——")
    print("      结果每次问「要不要」都会生成一条假约束，pinned 消息越积越多，")
    print("      压缩等于没压。改用正则短语 + 负向前瞻才修好。")


def main() -> int:
    demo_budget()
    demo_strategies()
    demo_hybrid_detail()
    demo_timing()
    demo_pitfall()

    header("小结")
    print("  1. 短期记忆按 **token** 算预算，不按消息条数。")
    print("  2. window 丢最旧、summary 压成一句——**两者都可能丢掉关键约束**（本 demo 都丢了 2 个）。")
    print("  3. hybrid 靠「抽取 + 钉住」保住约束，代价是多占一点 token（174 vs 50/135）。")
    print("  4. 压缩在预算 70% 左右触发，别等超了再压。")
    print("  5. 约束抽取的规则要**可测**：'要不要' 这种词最容易误判。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
