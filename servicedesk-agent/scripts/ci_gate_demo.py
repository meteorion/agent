# file: scripts/ci_gate_demo.py
"""5.4 的场景：把评测接进 CI，改坏了就让流水线变红。

用法：
    uv run python -m scripts.ci_gate_demo
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from servicedesk.eval.ci_gate import (
    DEFAULT_RULES,
    collect_metrics,
    compare,
    load_snapshot,
    render,
    save_snapshot,
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


def run_cli(*args: str) -> tuple[int, str]:
    """真跑 CLI 子进程——门禁的价值就在**退出码**上。"""
    completed = subprocess.run(
        [sys.executable, "-m", "servicedesk.eval.ci_gate", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def demo_rules() -> None:
    header("第 1 步：先看规则 —— 哪些指标能容忍回退")
    print(f"  {pad('指标', 30)}{pad('下限', 10)}{pad('最大跌幅', 12)}级别")
    print("  " + "-" * 62)
    for rule in DEFAULT_RULES:
        floor = f"{rule.floor:.2f}" if rule.floor is not None else "—"
        print(f"  {pad(rule.name, 30)}{pad(floor, 10)}"
              f"{pad(f'{rule.max_drop:.2f}', 12)}{rule.severity}")
    print()
    print("  >>> 安全类（tool_order / authorization / injection / pii）下限是 1.00、")
    print("      最大跌幅 0.00——**一次都不许退**。")
    print("      质量类（pass_rate）允许 5% 波动，因为评测本身有噪声。")


def demo_baseline(tmp: Path) -> Path:
    header("第 2 步：建立基线快照")
    baseline_path = tmp / "baseline.json"

    metrics = collect_metrics()
    save_snapshot(baseline_path, metrics)

    print(f"  跑齐 5.1–5.3 三套评测，写入 {baseline_path.name}：")
    for name, value in sorted(metrics.items()):
        print(f"    {pad(name, 30)}{value:.3f}")
    print()
    print("  >>> baseline.json 要**提交进 git**：它代表「我们认为合格的水平」。")
    print("      改基线必须走 code review——否则谁都能把门槛降下来。")
    return baseline_path


def demo_pass(baseline_path: Path) -> None:
    header("第 3 步：正常提交 —— 流水线变绿")
    code, output = run_cli("--baseline", str(baseline_path))
    print(f"  退出码: {code}   （0 = 绿）")
    print()
    for line in output.splitlines()[:8]:
        print(f"  {line}")
    print()
    print("  >>> CI 只看退出码：0 放行，非 0 拦住。")


def demo_fail(baseline_path: Path) -> None:
    header("第 4 步：故意改坏 —— 流水线变红")
    print("  模拟三种「改坏」：")
    print("    ① 通用评测：一次改动让通过率掉 25%")
    print("    ② RAG：top_k 调太大，混入噪声")
    print("    ③ Agent：跳过审批、被注入带走、泄漏 PII")
    print()

    code, output = run_cli("--baseline", str(baseline_path), "--broken")
    print(f"  退出码: {code}   （非 0 = 红，CI 会拦住这次合并）")
    print()
    for line in output.splitlines()[:30]:
        print(f"  {line}")

    print()
    print("  >>> 注意每条回退都指出**基线值 → 当前值 → 跌幅 → 原因**。")
    print("      工程师不需要翻日志，看完这段就知道哪里坏了。")


def demo_recover(baseline_path: Path) -> None:
    header("第 5 步：改回来 —— 流水线恢复变绿")
    code, output = run_cli("--baseline", str(baseline_path))
    print(f"  退出码: {code}")
    print(f"  {output.splitlines()[0]}")
    print()
    print("  >>> 这就是闭环：**改坏 → 红 → 修好 → 绿**。不需要任何人记得去跑评测。")


def demo_edge_cases(baseline_path: Path) -> None:
    header("第 6 步：两种特殊情况")

    print("  ① 指标缺失（评测没跑全）")
    baseline = load_snapshot(baseline_path)
    partial = {k: v for k, v in baseline.items() if not k.startswith("agent.")}
    decision = compare(partial, baseline)
    print(f"     结果: {'PASS' if decision.passed else 'FAIL'}，缺失 {decision.missing}")
    print("     >>> 缺指标按失败处理——「评测没跑」和「评测失败」一样危险，")
    print("         因为你会误以为一切正常。")

    print()
    print("  ② 阈值放宽（临时例外）")
    lenient = compare(collect_metrics(broken=True), baseline, [
        rule.__class__(rule.name, rule.floor, 0.5, "warn") for rule in DEFAULT_RULES
    ])
    print(f"     结果: {'PASS' if lenient.passed else 'FAIL'}"
          f"（全部降级为 warn、跌幅容忍 0.5）")
    print("     >>> 放宽必须**显式写进 diff**，让 review 看见；")
    print("         不要偷偷改 DEFAULT_RULES。")


def demo_workflow() -> None:
    header("第 7 步：GitHub Actions 里怎么接")
    workflow = """name: eval
on: [pull_request]

jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --frozen
      - name: 评测门禁
        run: uv run python -m servicedesk.eval.ci_gate --baseline eval/baseline.json
      - name: 归档报告
        if: always()
        run: uv run python -m servicedesk.eval.ci_gate \\
             --baseline eval/baseline.json --json > eval-report.json
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: eval-report
          path: eval-report.json"""
    for line in workflow.splitlines():
        print(f"  {line}")
    print()
    print("  >>> 三个要点：")
    print("      1. 门禁步骤失败 → job 失败 → PR 不能合并")
    print("      2. 报告用 `if: always()` 归档，**红了也要能看到报告**")
    print("      3. baseline 在仓库里，改它必须走 review")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ci-gate-"))

    demo_rules()
    baseline_path = demo_baseline(tmp)
    demo_pass(baseline_path)
    demo_fail(baseline_path)
    demo_recover(baseline_path)
    demo_edge_cases(baseline_path)
    demo_workflow()

    header("小结")
    print("  1. 基线快照进 git：它定义「合格」，改它要走 review。")
    print("  2. 门禁只看退出码：0 放行、非 0 拦住——不依赖任何人记得跑评测。")
    print("  3. 安全类阈值 1.00 + 最大跌幅 0.00；质量类允许小幅波动。")
    print("  4. 缺指标按失败处理：评测没跑和评测失败一样危险。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
