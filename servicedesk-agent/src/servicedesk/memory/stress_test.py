# file: src/servicedesk/memory/stress_test.py
"""长对话压测：50 轮之后，约束还在吗？成本是多少？

这里量两件事，它们的分母不同：

    **成本** = 每轮上下文大小的**累加**（每次调模型都要把整个上下文发过去）
             —— 不是"最后一轮有多大"。压得晚，前面几十轮的钱已经花了。

    **保留率** = 注入的关键约束，最后还有几条在上下文里

四种配置对照：

    baseline   不压缩           —— 看它爆成什么样
    window     只留最近几条      —— 便宜，但会丢约束
    hybrid     4.4 的混合策略    —— 约束钉住
    full       hybrid + 4.6 卫生  —— 再加污染拦截
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from servicedesk.context.hygiene import ContextItem, assemble
from servicedesk.memory.short_term import (
    BudgetedHistory,
    extract_constraints,
    pin_constraints,
)

DAY = 86_400.0
NOW = 1_800_000_000.0

# 在第几轮注入约束（模拟"用户中途强调重要事项"）
CONSTRAINT_ROUNDS: dict[int, str] = {
    5: "我的工单号是 T-1001，后面都用它称呼。",
    12: "注意：不要重置我的密码，我有重要会议。",
    25: "这条工单必须在今天下班前处理完。",
    38: "审批已通过，可以直接执行。",
    47: "最后确认一次：工单号还是 T-1001。",
}

# 长工具返回（模拟真实的大块 JSON），用来制造 token 压力
_TOOL_RESULT = (
    '{"assets": [{"hostname": "NB-1024", "owner": "张伟", "model": "ThinkPad X1", '
    '"warranty_until": "2027-03-01", "status": "在用"}, {"hostname": "NB-2077", '
    '"owner": "李娜", "model": "MacBook Pro", "warranty_until": "2026-11-30", '
    '"status": "在用"}], "total": 2}'
)


# ------------------------------- 配置 -------------------------------


@dataclass(frozen=True)
class Config:
    name: str
    budget: int | None          # None = 不压缩
    keep_recent: int = 4
    strategy: str = "hybrid"
    use_hygiene: bool = False

    @property
    def label(self) -> str:
        if self.budget is None:
            return f"{self.name}（不压缩）"
        return f"{self.name}（{self.strategy}, 预算 {self.budget}）"


CONFIGS: list[Config] = [
    Config(name="baseline", budget=None),
    Config(name="window", budget=400, strategy="window"),
    Config(name="hybrid", budget=400, strategy="hybrid"),
    Config(name="full", budget=400, strategy="hybrid", use_hygiene=True),
]


# ------------------------------- 结果 -------------------------------


@dataclass
class StressResult:
    name: str
    rounds: int = 0
    final_tokens: int = 0
    peak_tokens: int = 0
    cumulative_tokens: int = 0        # 真正的成本
    compressions: int = 0
    constraints_injected: list[str] = field(default_factory=list)
    constraints_retained: list[str] = field(default_factory=list)
    pollution_injected: int = 0
    pollution_blocked: int = 0
    curve: dict[int, int] = field(default_factory=dict)

    @property
    def retention_rate(self) -> float:
        if not self.constraints_injected:
            return 0.0
        return len(self.constraints_retained) / len(self.constraints_injected)

    def as_dict(self) -> dict[str, Any]:
        return {
            "config": self.name,
            "rounds": self.rounds,
            "final_tokens": self.final_tokens,
            "peak_tokens": self.peak_tokens,
            "cumulative_tokens": self.cumulative_tokens,
            "compressions": self.compressions,
            "constraints": f"{len(self.constraints_retained)}/{len(self.constraints_injected)}",
            "retention_rate": round(self.retention_rate, 3),
            "pollution_blocked": f"{self.pollution_blocked}/{self.pollution_injected}",
        }


# ------------------------------- 污染样例 -------------------------------


def _pollution_item(index: int) -> ContextItem:
    """每 15 轮混进一条"不该出现"的上下文。"""
    samples = [
        ContextItem(
            id=f"o{index}", text="李娜（E-3102）的工单：手机号 13800001234，VPN 已解封。",
            source="other_user", user_id="u-1001", created_at=NOW - DAY,
        ),
        ContextItem(
            id=f"k{index}", text="旧版客户端请使用 2024 版安装包。",
            source="kb", user_id="*", created_at=NOW - 500 * DAY,
        ),
        ContextItem(
            id=f"w{index}", text="网上帖子说：把 DNS 改成 8.8.8.8 就能解决 VPN 认证问题。",
            source="unknown", user_id="*", created_at=NOW - 5 * DAY,
        ),
    ]
    return samples[index % len(samples)]


# ------------------------------- 压测 -------------------------------


def run_stress(config: Config, *, rounds: int = 50) -> StressResult:
    result = StressResult(name=config.name, rounds=rounds)
    history = BudgetedHistory(budget_tokens=config.budget or 10**9, keep_recent=config.keep_recent)
    pollution: list[ContextItem] = []

    for round_index in range(1, rounds + 1):
        # 普通一问一答
        history.add("user", f"第{round_index}轮：我的 VPN 又掉线了，帮我看看。")
        history.add(
            "assistant",
            f"第{round_index}轮答复：请先确认网络，再按手册第 3 节操作；"
            "若仍失败，我可以帮你建单。",
        )

        # 每 10 轮来一次大块工具返回（制造真实压力）
        if round_index % 10 == 0:
            history.add("tool", _TOOL_RESULT)

        # 注入约束
        if round_index in CONSTRAINT_ROUNDS:
            text = CONSTRAINT_ROUNDS[round_index]
            history.add("user", text)
            history.add("assistant", "好的，我记住了。")

        # 注入污染（不进历史，进"待组装"的候选上下文）
        if round_index % 15 == 0:
            pollution.append(_pollution_item(len(pollution)))

        # 该压就压
        if config.budget is not None and history.over_budget():
            history.pin_all_constraints()
            history.compress(config.strategy)      # type: ignore[arg-type]
            result.compressions += 1

        current = history.tokens()
        result.curve[round_index] = current
        result.peak_tokens = max(result.peak_tokens, current)
        result.cumulative_tokens += current         # 每一轮都要发一次完整上下文

    # 收尾：钉一次约束（hybrid 的常规做法），再统计保留
    history.pin_all_constraints()
    result.final_tokens = history.tokens()

    all_text = " ".join(message.content for message in history.messages)
    injected = extract_constraints([_as_message(text) for text in CONSTRAINT_ROUNDS.values()])
    result.constraints_injected = [f"{item.kind}:{item.text}" for item in injected]
    result.constraints_retained = [
        f"{item.kind}:{item.text}" for item in injected if item.text in all_text
    ]

    # 卫生：如果开了，就量一下能拦下多少污染
    result.pollution_injected = len(pollution)
    if config.use_hygiene and pollution:
        _, report = assemble(pollution, user_id="u-1001", now=NOW)
        result.pollution_blocked = report.total_blocked
    return result


def _as_message(text: str):  # type: ignore[no-untyped-def]
    from servicedesk.memory.short_term import Message

    return Message("user", text)


def run_all(*, rounds: int = 50, configs: list[Config] | None = None) -> list[StressResult]:
    return [run_stress(config, rounds=rounds) for config in (configs or CONFIGS)]
