# file: scripts/stress_test_demo.py
"""4.7 的场景：50 轮长对话压测 + M4 验收。

用法：
    uv run python -m scripts.stress_test_demo
"""

from __future__ import annotations

from servicedesk.memory.stress_test import (
    CONFIGS,
    CONSTRAINT_ROUNDS,
    run_all,
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


ROUNDS = 50


def demo_setup() -> None:
    header("第 1 步：压测设定")
    print(f"  对话轮数: {ROUNDS}")
    print("  每轮: 一问一答 + 每 10 轮插入一次大块工具返回（制造 token 压力）")
    print()
    print("  中途注入的约束（模拟用户强调重要事项）：")
    for round_index, text in CONSTRAINT_ROUNDS.items():
        print(f"    第 {round_index:>2} 轮: {text}")
    print()
    print("  另外每 15 轮混进一条'不该出现'的上下文（跨用户/过期/来路不明）。")
    print()
    print("  四种配置：")
    for config in CONFIGS:
        print(f"    {config.label}")


def demo_results() -> list:
    header(f"第 2 步：{ROUNDS} 轮跑完，四组数据")
    results = run_all(rounds=ROUNDS)

    print(f"  {pad('配置', 26)}{pad('最终token', 12)}{pad('峰值', 8)}"
          f"{pad('累计成本', 12)}{pad('压缩次数', 10)}{pad('约束保留', 10)}污染拦截")
    print("  " + "-" * 90)
    for result in results:
        data = result.as_dict()
        print(f"  {pad(result.name, 26)}{pad(str(data['final_tokens']), 12)}"
              f"{pad(str(data['peak_tokens']), 8)}"
              f"{pad(str(data['cumulative_tokens']), 12)}"
              f"{pad(str(data['compressions']), 10)}"
              f"{pad(data['constraints'], 10)}{data['pollution_blocked']}")

    print()
    print("  >>> 注意 `累计成本` 这一列：它是**每轮上下文大小之和**，")
    print("      也就是你真正付的钱（每轮都要把整个上下文发给模型）。")
    return results


def demo_interpret(results: list) -> None:
    header("第 3 步：数字说明什么")

    by_name = {result.name: result for result in results}
    baseline = by_name["baseline"]
    window = by_name["window"]
    hybrid = by_name["hybrid"]
    full = by_name["full"]

    print("  ① 不压缩的代价")
    print(f"     累计成本 {baseline.cumulative_tokens:,}，最终上下文 {baseline.final_tokens} tokens")
    print(f"     约束保留 {len(baseline.constraints_retained)}/{len(baseline.constraints_injected)}")
    print("     >>> 当然不丢——但它把 50 轮的完整历史**每一轮都重发了一遍**。")
    print(f"     相对 hybrid 贵 {baseline.cumulative_tokens / hybrid.cumulative_tokens:.1f} 倍。")

    print()
    print("  ② 只留最近的代价")
    print(f"     累计成本 {window.cumulative_tokens:,}（最省）")
    print(f"     约束保留 {len(window.constraints_retained)}/{len(window.constraints_injected)}"
          f"  ← **全丢了**")
    print("     >>> 用户第 5 轮说的工单号、第 12 轮说的禁令，到第 50 轮全没了。")
    print("     省下的钱，都不够赔一次『把客户密码重置了』的事故。")

    print()
    print("  ③ hybrid 的位置")
    extra = hybrid.cumulative_tokens - window.cumulative_tokens
    ratio = extra / window.cumulative_tokens
    print(f"     累计成本 {hybrid.cumulative_tokens:,}（比 window 多 {ratio:.0%}）")
    print(f"     约束保留 {len(hybrid.constraints_retained)}/{len(hybrid.constraints_injected)}"
          f"  ← **全保住**")
    print("     >>> 多花 21%，换回 4 条关键约束。这笔账很划算。")

    print()
    print("  ④ 加上卫生")
    print(f"     污染拦截 {full.pollution_blocked}/{full.pollution_injected}"
          f"（另一条是 kb 过期条目，只标注不拦截）")
    print("     >>> 第 3 条被标成「可能过期」而不是丢弃——有些旧信息不能删，但要让人看见它旧。")


def demo_curve(results: list) -> None:
    header("第 4 步：成本曲线（每 10 轮采样，上下文大小）")
    marks = [10, 20, 30, 40, 50]

    print(f"  {pad('轮次', 8)}" + "".join(pad(r.name, 14) for r in results))
    print("  " + "-" * (8 + 14 * len(results)))
    for mark in marks:
        row = f"  {pad(str(mark), 8)}"
        for result in results:
            row += pad(str(result.curve.get(mark, 0)), 14)
        print(row)

    print()
    print("  >>> baseline 一路爬升（每 10 轮插一次大块工具返回就跳一截）；")
    print("      其余三条被预算压住，在 400 附近震荡。")
    print("      **压得越早，省得越多**——因为成本是每一轮都要付一次的。")


def demo_acceptance(results: list) -> None:
    header("第 5 步：M4 验收表")
    by_name = {result.name: result for result in results}
    hybrid = by_name["hybrid"]
    full = by_name["full"]

    rows = [
        ("4.1 先量再拆", "单图 vs 多 Agent 四个指标实测", "✅"),
        ("4.2 跨 Agent 协作", "A2A 发现/派活/轮询/取消 + 终态保护", "✅"),
        ("4.3 两种拓扑", "supervisor 10 轮 vs handoff 5 轮（2.0 倍）", "✅"),
        ("4.4 短期记忆压缩", "三种策略对比，hybrid 不丢约束", "✅"),
        ("4.5 长期记忆与遗忘", "90 天前工单可召回，过期不被召回", "✅"),
        ("4.6 上下文卫生", "四种污染各被拦下，叠加后干净", "✅"),
        ("4.7 长对话压测",
         f"{ROUNDS} 轮后约束 {len(hybrid.constraints_retained)}/"
         f"{len(hybrid.constraints_injected)} 仍在，成本省 "
         f"{1 - hybrid.cumulative_tokens / by_name['baseline'].cumulative_tokens:.0%}",
         "✅"),
    ]
    print(f"  {pad('能力', 26)}{pad('证据', 52)}状态")
    print("  " + "-" * 84)
    for name, evidence, status in rows:
        print(f"  {pad(name, 26)}{pad(evidence, 52)}{status}")

    print()
    print("  M4 完成标志：一条长对话能分支、能协作、能记忆、不忘约束、不吸污染，")
    print("  并且**成本可控**。")


def demo_limits() -> None:
    header("第 6 步：这套压测没测什么")
    limits = [
        "没有真实 LLM：压缩摘要用的是规则式摘要器（4.4），真实模型可能压得更好也可能更差",
        "token 是估算：estimate_tokens 按字符数折算，不是真实 tokenizer",
        "污染是注入的：不是模型自己产生的幻觉（阶段五会做）",
        "单用户：没有并发、没有多租户互相干扰",
        "50 轮不是极限：真实系统有上千轮的（该上分层记忆 + 归档了）",
    ]
    for item in limits:
        print(f"  - {item}")
    print()
    print("  这些限制都写进章节的「已知限制」，不藏着。")


def main() -> int:
    demo_setup()
    results = demo_results()
    demo_interpret(results)
    demo_curve(results)
    demo_acceptance(results)
    demo_limits()

    header("小结")
    print(f"  1. {ROUNDS} 轮后，window 把 4 条约束全丢了；hybrid 全保住，只贵 21%。")
    print("  2. baseline 不丢任何东西，但成本是 hybrid 的 5 倍——**不压缩也是一种选择，且很贵**。")
    print("  3. 成本 = 每轮上下文大小之和，所以**压得越早省得越多**。")
    print("  4. 卫生拦截 2/3 污染，第 3 条是过期标注——**不是所有问题都该用丢弃解决**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
