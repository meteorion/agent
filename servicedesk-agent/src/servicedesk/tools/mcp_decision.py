# file: src/servicedesk/tools/mcp_decision.py
"""判断某个能力"该不该 MCP 化"。

把 docs/mcp-notes.md 里的决策四问写成可运行的代码，
这样"我觉得该做"就变成了"算一遍 + 说出理由"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 协议本身的固定成本：学规范、调通握手、维护版本兼容。
# 用"相当于几个适配器"来折算，方便和 M×N 直接比较。
PROTOCOL_OVERHEAD = 1


@dataclass(frozen=True)
class Scenario:
    """一个待评估的能力。"""

    key: str
    title: str
    consumers: int              # M：有几个应用/团队要用
    systems: int                # N：背后要接几个系统
    needs_isolation: bool = False   # 是否需要独立部署/升级/鉴权
    third_party: bool = False       # 是否要给外部方（跨组织/跨语言）用
    note: str = ""


@dataclass(frozen=True)
class Decision:
    scenario: Scenario
    suggest: bool
    adapters_custom: int        # 各自定制：M × N
    adapters_mcp: int           # 统一协议：M + N + 协议成本
    reasons: list[str] = field(default_factory=list)

    @property
    def savings(self) -> int:
        return self.adapters_custom - self.adapters_mcp

    @property
    def verdict(self) -> str:
        if self.suggest and not self.reasons_arithmetic:
            return "该做（业务理由优先，算数上不占优）"
        if self.suggest and self.savings == 0:
            return "该做（算数打平，业务理由补足）"
        if self.suggest:
            return "该做"
        return "不该做"

    @property
    def reasons_arithmetic(self) -> bool:
        return self.adapters_custom > self.adapters_mcp


def decide(scenario: Scenario) -> Decision:
    """按决策四问给出结论与理由。"""
    custom = scenario.consumers * scenario.systems
    mcp = scenario.consumers + scenario.systems + PROTOCOL_OVERHEAD
    reasons: list[str] = []

    # 问 1：有多个消费方吗？
    multi_consumer = scenario.consumers >= 2
    reasons.append(
        f"问1 消费方 {scenario.consumers} 个 → "
        + ("✅ 存在重复实现风险" if multi_consumer else "❌ 只有自己用，抽象是浪费")
    )

    # 问 2：需要独立部署/鉴权吗？
    reasons.append(
        "问2 独立部署/鉴权 → "
        + ("✅ 需要" if scenario.needs_isolation else "❌ 不需要，同生共死即可")
    )

    # 问 3：要给外部方用吗？
    reasons.append(
        "问3 跨组织/跨语言调用 → "
        + ("✅ 需要，协议是唯一安全入口" if scenario.third_party else "❌ 不需要")
    )

    # 问 4：算数划算吗？
    if custom > mcp:
        arithmetic_text = "✅ 协议更少"
    elif custom == mcp:
        arithmetic_text = "⚖️ 打平（还要算上协议学习成本，实际略亏）"
    else:
        arithmetic_text = f"❌ 协议更多（多 {mcp - custom} 个）"
    reasons.append(f"问4 适配器 {custom}(M×N) vs {mcp}(M+N+{PROTOCOL_OVERHEAD}) → {arithmetic_text}")

    # 结论：业务理由优先；算数占优是加分项，不是必需项
    must = scenario.third_party or (scenario.needs_isolation and multi_consumer)
    arithmetic_win = custom > mcp
    suggest = must or (arithmetic_win and multi_consumer and scenario.systems >= 1)

    if must and not arithmetic_win:
        reasons.append("结论：业务理由（跨组织/独立鉴权）压倒算数，必须上协议")
    elif suggest and not arithmetic_win:
        reasons.append("结论：算数打平，但业务理由成立")
    elif suggest:
        reasons.append("结论：算数与业务理由同时成立")
    else:
        reasons.append("结论：建议先写普通函数，等出现第二个消费方再抽象")

    return Decision(
        scenario=scenario,
        suggest=suggest,
        adapters_custom=custom,
        adapters_mcp=mcp,
        reasons=reasons,
    )


# ------------------------------ 预置场景 ------------------------------

SCENARIOS: list[Scenario] = [
    Scenario(
        key="A",
        title="工单查询能力（3 个应用 × 2 个系统）",
        consumers=3,
        systems=2,
        needs_isolation=True,
        note="客服助手、工单机器人、内部 CLI 都要用；鉴权按角色区分字段",
    ),
    Scenario(
        key="B",
        title="格式化日期 / 计算 SLA（1 个应用 × 0 个系统）",
        consumers=1,
        systems=0,
        note="纯本地逻辑，没有后端系统",
    ),
    Scenario(
        key="C",
        title="资产只读查询，要给合作方访问（2 个消费方 × 1 个系统）",
        consumers=2,
        systems=1,
        needs_isolation=True,
        third_party=True,
        note="合作方运维平台需要跨组织接入，只看自己的设备",
    ),
    Scenario(
        key="D",
        title="单个助手的私有小工具（1 个应用 × 5 个系统）",
        consumers=1,
        systems=5,
        note="只有你自己的助手在用，别人不会接",
    ),
    Scenario(
        key="E",
        title="公司级监控查询（5 个应用 × 5 个系统）",
        consumers=5,
        systems=5,
        needs_isolation=True,
        note="典型的多应用多系统，重复实现严重",
    ),
]
