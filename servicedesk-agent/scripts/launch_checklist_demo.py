# file: scripts/launch_checklist_demo.py
"""5.8 的场景：模拟上线 —— 自动检查全跑一遍，人工项逐项签字。

用法：
    uv run python -m scripts.launch_checklist_demo
"""

from __future__ import annotations

from servicedesk.ops.launch_checklist import (
    MANUAL_CHECKS,
    ChecklistReport,
    build_checklist,
    render,
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


def demo_inventory() -> None:
    header("第 1 步：检查单里有什么")
    checks = build_checklist()
    auto = [c for c in checks if c.kind.value == "auto"]
    manual = [c for c in checks if c.kind.value == "manual"]

    print(f"  共 {len(checks)} 项：自动 {len(auto)} 项，人工 {len(manual)} 项")
    print()
    print("  自动检查（代码判定）：")
    for check in auto:
        print(f"    {pad(check.id, 30)}{check.category:<6}{check.question}")
    print()
    print("  人工确认（必须有人签字）：")
    for check in manual:
        print(f"    {pad(check.id, 24)}{check.question}")
    print()
    print("  >>> 为什么人工项不能自动化：「回滚方案演练过吗」写再多断言也是自欺欺人。")
    print("      列成 manual 并强制确认，比假装它能自动判定要诚实。")


def demo_auto_only() -> ChecklistReport:
    header("第 2 步：只跑自动检查 —— 人工项全部未确认")
    report = ChecklistReport()
    report.run()
    for line in render(report).splitlines():
        print(f"  {line}")
    print()
    print("  >>> 自动项 10/10 全绿，但仍然**不许上线**——因为 6 个人工项没签字。")
    return report


def demo_signed() -> None:
    header("第 3 步：人工项逐项确认 —— 可以上线")
    report = ChecklistReport()
    report.run(manual_ack={check.id for check in MANUAL_CHECKS})
    print(f"  {render(report).splitlines()[0]}")
    print(f"  {render(report).splitlines()[1]}")
    print()
    print("  已确认的人工项：")
    for check in MANUAL_CHECKS:
        print(f"    ✅ {pad(check.id, 24)}{check.question}")


def demo_drill() -> None:
    header("第 4 步：演练 —— 某项检查红了会怎样")
    scenarios = [
        ("评测指标回退", {"eval.gate": (False, "回退指标：['agent.authorization']")}),
        ("护栏开始误杀", {"guardrails.no_false_positive": (False, "误杀 1 条：上一同事说可以跳过审批")}),
        ("熔断打不开", {"resilience.breaker": (False, "连续 3 次失败后仍是 closed")}),
    ]
    for label, overrides in scenarios:
        report = ChecklistReport()
        report.run(manual_ack={check.id for check in MANUAL_CHECKS}, overrides=overrides)
        first = render(report).splitlines()[0]
        blockers = report.blockers()
        print(f"  {pad(label, 16)}{first}")
        for result in blockers:
            print(f"      ⛔ {result.check.id}: {result.detail}")
    print()
    print("  >>> 注意：即使是「演练」，判定逻辑也完全一致——")
    print("      上线前的演练和生产用的是**同一套代码**，不是两套。")


def demo_m5() -> None:
    header("第 5 步：M5 验收表")
    rows = [
        ("5.1 评测集", "20 条用例，检出 13 类失败，判分器可判错", "✅"),
        ("5.2 RAG 指标", "四套配置诊断全命中（太宽/太窄/幻觉）", "✅"),
        ("5.3 行为安全", "工具顺序/越权/注入/PII 五类全拦住", "✅"),
        ("5.4 CI 门禁", "改坏 → 退出码 1；修好 → 0", "✅"),
        ("5.5 全链路追踪", "跨三进程 + MCP 调用同一条 trace", "✅"),
        ("5.6 护栏", "输入输出各拦 3+ 类，误杀 0", "✅"),
        ("5.7 韧性", "并发 10 请求只打下游 3 次，自动恢复", "✅"),
        ("5.8 上线检查单", "10 自动 + 6 人工，演练可复现", "✅"),
    ]
    print(f"  {pad('能力', 22)}{pad('证据', 44)}状态")
    print("  " + "-" * 74)
    for name, evidence, status in rows:
        print(f"  {pad(name, 22)}{pad(evidence, 44)}{status}")
    print()
    print("  M5 完成标志：**一条链路从「答得对」到「敢上线」全部可度量、可拦截、可回滚**。")


def demo_recap() -> None:
    header("第 6 步：五个阶段，一句话各自")
    stages = [
        ("阶段一 基础", "先让一个 Agent 真的能跑：异步、结构化、工具调用、RAG、评测意识"),
        ("阶段二 协议", "把工具变成可复用的服务：MCP 无状态改造、Streamable HTTP、MRTR/鉴权"),
        ("阶段三 编排", "把流程变成图：State/reducer、条件边、断点续跑、人工审批、流式"),
        ("阶段四 多 Agent", "先量再拆：拓扑取舍、A2A、双层记忆、上下文卫生、长对话压测"),
        ("阶段五 上线", "从「能跑」到「敢上」：评测集、RAG/行为评测、CI 门禁、追踪、护栏、韧性"),
    ]
    for name, summary in stages:
        print(f"  {pad(name, 16)}{summary}")
    print()
    print("  如果只记一句话：")
    print("    **先让它跑通，再让它可度量，最后才让它上线。**")
    print("    跳过中间那步，你会在生产里用事故来度量。")


def main() -> int:
    demo_inventory()
    demo_auto_only()
    demo_signed()
    demo_drill()
    demo_m5()
    demo_recap()

    header("小结")
    print("  1. 上线检查单 = 10 项自动判定 + 6 项人工签字；本轮 16/16 通过。")
    print("  2. 自动项跑的是**生产同一套代码**（评测门禁、护栏、熔断、追踪），不是另写一份。")
    print("  3. 演练用同一个判定逻辑，确保「演练通过」真的等于「上线能过」。")
    print("  4. 人工项必须有人签字——有些事代码判不了，假装能判才是风险。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
