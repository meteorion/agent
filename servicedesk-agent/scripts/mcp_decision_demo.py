# file: scripts/mcp_decision_demo.py
"""2.2 的场景：把「该不该 MCP 化」算出来，而不是凭感觉。

用法：
    uv run python -m scripts.mcp_decision_demo
"""

from __future__ import annotations

from servicedesk.tools.mcp_decision import SCENARIOS, Scenario, decide


def header(title: str) -> None:
    print()
    print("=" * 76)
    print(title)
    print("=" * 76)


def show(scenario: Scenario) -> None:
    decision = decide(scenario)
    flag = "✅ 该做" if decision.suggest else "❌ 不该做"
    print(f"\n【场景 {scenario.key}】{scenario.title}")
    print(f"  M(消费方)={scenario.consumers}  N(系统)={scenario.systems}"
          f"  独立鉴权={scenario.needs_isolation}  跨组织={scenario.third_party}")
    for reason in decision.reasons:
        print(f"    {reason}")
    print(f"  → 适配器：各自定制 {decision.adapters_custom} 个 vs 统一协议 {decision.adapters_mcp} 个"
          f"（差 {decision.savings:+d}）")
    print(f"  → {flag}：{decision.verdict}")
    if scenario.note:
        print(f"  → 业务背景：{scenario.note}")


def main() -> int:
    header("第 1 步：适配器数量的算数")
    print("  各自定制 = M × N；统一协议 = M + N + 协议成本(1)")
    print(f"  {'M':>3}{'N':>4}{'各自定制':>10}{'统一协议':>10}{'差':>6}   结论")
    for consumers in (1, 2, 3, 5):
        for systems in (0, 1, 2, 5):
            custom = consumers * systems
            mcp = consumers + systems + 1
            mark = "协议更省" if custom > mcp else ("打平" if custom == mcp else "协议更贵")
            print(f"  {consumers:>3}{systems:>4}{custom:>10}{mcp:>10}{custom - mcp:>6}   {mark}")

    header("第 2 步：五个场景的判断")
    for scenario in SCENARIOS:
        show(scenario)

    header("小结")
    print("  1. MCP 是解决 M×N 的收敛手段，M 或 N 为 1 时它只会增加成本。")
    print("  2. 算数是必要条件，不是充分条件：跨组织/独立鉴权可以压倒算数。")
    print("  3. 正确顺序：先写普通函数 → 出现第二个消费方 → 再抽成 MCP。")
    print("  4. Tools 是动作（模型发起），Resources 是数据（Host 发起），")
    print("     Prompts 是模板（用户发起）。把只读数据做成 Tool 是最常见的误用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
