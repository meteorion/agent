# file: scripts/run_all.py
"""一条命令跑完全部验证：571 个测试 + 12 个 demo。

用法：
    uv run python -m scripts.run_all            # 全部
    uv run python -m scripts.run_all --fast     # 跳过耗时 demo
    uv run python -m scripts.run_all --list     # 只列出要跑什么

为什么需要它：教程 37 节各有一个 demo，逐个手敲容易漏。
这个入口把"全部演示都能跑通"变成**一个可判定的动作**（退出码 0/1）。
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Demo:
    module: str
    chapter: str
    description: str
    slow: bool = False          # 慢的（十几秒那种）在 --fast 下跳过


DEMOS: list[Demo] = [
    Demo("scripts.async_demo", "1.2", "异步：并发 vs 串行", slow=True),
    Demo("scripts.llm_demo", "1.3", "亲手发一次 LLM 请求"),
    Demo("scripts.ticket_demo", "1.4", "结构化输出与校验"),
    Demo("scripts.resilience_demo", "1.5", "重试与错误分层"),
    Demo("scripts.split_demo", "1.7", "切分策略"),
    Demo("scripts.rag_demo", "1.7", "检索与重排"),
    Demo("scripts.ask", "1.8", "证据门与引用"),
    Demo("scripts.function_calling_demo", "2.1", "工具循环"),
    Demo("scripts.mcp_decision_demo", "2.2", "要不要用 MCP"),
    Demo("scripts.mcp_stateless_demo", "2.3", "无状态往返"),
    Demo("scripts.mcp_server_demo", "2.4", "stdio Server"),
    Demo("scripts.mcp_http_demo", "2.5", "Streamable HTTP + 订阅流"),
    Demo("scripts.mrtr_client", "2.6", "MRTR + OAuth"),
    Demo("scripts.m2_agent", "2.7", "M2 端到端（适配器）"),
    Demo("scripts.langgraph_intro_demo", "3.1", "从 if 到状态图"),
    Demo("scripts.state_design_demo", "3.2", "State 与 reducer"),
    Demo("scripts.edges_demo", "3.3", "条件边与循环"),
    Demo("scripts.checkpoint_demo", "3.4", "断点续跑（真杀进程）"),
    Demo("scripts.approval_demo", "3.5", "审批挂起与恢复"),
    Demo("scripts.streaming_demo", "3.6", "流式与可观测", slow=True),
    Demo("scripts.composite_demo", "3.7", "子图与 M3 验收"),
    Demo("scripts.single_vs_multi_demo", "4.1", "单图 vs 多 Agent"),
    Demo("scripts.a2a_demo", "4.2", "A2A 跨进程"),
    Demo("scripts.topologies_demo", "4.3", "两种拓扑"),
    Demo("scripts.short_term_memory_demo", "4.4", "短期记忆压缩"),
    Demo("scripts.long_term_memory_demo", "4.5", "长期记忆与遗忘"),
    Demo("scripts.context_hygiene_demo", "4.6", "上下文卫生"),
    Demo("scripts.stress_test_demo", "4.7", "50 轮压测"),
    Demo("scripts.eval_dataset_demo", "5.1", "评测集与判分"),
    Demo("scripts.rag_eval_demo", "5.2", "RAG 指标与病根"),
    Demo("scripts.agent_eval_demo", "5.3", "行为与安全"),
    Demo("scripts.ci_gate_demo", "5.4", "CI 门禁闭环"),
    Demo("scripts.tracing_demo", "5.5", "全链路 trace"),
    Demo("scripts.guardrails_demo", "5.6", "双向护栏"),
    Demo("scripts.launch_checklist_demo", "5.8", "上线检查单"),
]


def run(cmd: list[str]) -> tuple[int, float]:
    started = time.perf_counter()
    completed = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return completed.returncode, time.perf_counter() - started


def main(argv: list[str]) -> int:
    fast = "--fast" in argv
    listing = "--list" in argv

    demos = [demo for demo in DEMOS if not (fast and demo.slow)]
    if listing:
        print(f"将运行 {len(demos)} 个 demo + 全部测试"
              f"{'（--fast：跳过耗时 demo）' if fast else ''}：")
        for demo in demos:
            print(f"  {demo.chapter:<5}{demo.module:<40}{demo.description}")
        return 0

    print("=" * 72)
    print(f"跑全部验证：{len(demos)} 个 demo + pytest")
    print("=" * 72)

    failures: list[tuple[str, int]] = []
    total_started = time.perf_counter()

    for index, demo in enumerate(demos, 1):
        code, elapsed = run([sys.executable, "-m", demo.module])
        mark = "✅" if code == 0 else "❌"
        print(f"  {mark} [{index:>2}/{len(demos)}] {demo.chapter:<5}{demo.module:<40}"
              f"{elapsed:5.1f}s  {demo.description}")
        if code != 0:
            failures.append((demo.module, code))

    print("-" * 72)
    code, elapsed = run([sys.executable, "-m", "pytest", "-q"])
    mark = "✅" if code == 0 else "❌"
    print(f"  {mark} [测试] pytest -q{'':<30}{elapsed:5.1f}s  单元测试")
    if code != 0:
        failures.append(("pytest", code))

    total = time.perf_counter() - total_started
    print("=" * 72)
    if failures:
        print(f"❌ 失败 {len(failures)} 项，总耗时 {total:.1f}s")
        for name, rc in failures:
            print(f"    {name} → 退出码 {rc}")
        return 1
    print(f"✅ 全部通过：{len(demos)} 个 demo + pytest，总耗时 {total:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
