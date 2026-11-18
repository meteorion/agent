# file: src/servicedesk/context/hygiene.py
"""上下文卫生：四种污染路径 + 四种解法。

四条污染路径，都很常见：

    ① 跨用户：A 的记忆/工单混进 B 的上下文
    ② 跨会话：上一次的（错误）结论被当成事实带过来
    ③ 过期：旧版本文档、已失效的政策
    ④ 错误结论：低置信度、来自失败尝试的结论被当成结论

四种解法（**叠加使用，不是四选一**）：

    1. 命名空间隔离   按 user_id / scope 过滤
    2. 时效与来源标注  标注年龄与权威度，过期降权
    3. 来源权重       低权威来源直接丢弃或降级
    4. 冲突显式化     发现互相矛盾的条目，**原样呈现冲突**而不是偷偷选一个

最后一条最重要：**模型不怕看到冲突，怕的是你替它悄悄选了一个错的。**
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

DAY = 86_400.0

# 来源权威度（0–1）
SOURCE_WEIGHT: dict[str, float] = {
    "policy": 1.0,      # 公司政策，最高
    "kb": 0.8,          # 知识库
    "session": 0.5,     # 上一轮对话
    "memory": 0.4,      # 长期记忆
    "other_user": 0.0,  # 别人的东西，根本不该出现
}

# 低于这个权威度的条目直接丢弃
MIN_WEIGHT = 0.35
# 超过这个年龄的条目要打"过期"标记
STALE_DAYS = 180.0
# 低于这个置信度的条目不该被当成事实
MIN_CONFIDENCE = 0.5
# user_id 为这个值时表示"公共知识"（政策/知识库），不属于任何个人
SHARED_USER = "*"


# ------------------------------- 条目 -------------------------------


@dataclass
class ContextItem:
    id: str
    text: str
    source: str                 # policy / kb / session / memory / other_user
    user_id: str                # 这条信息属于谁
    created_at: float
    topic: str = ""             # 用于冲突检测：同一话题的不同说法
    confidence: float = 1.0     # 写入方自评的置信度
    version: int = 1            # 政策/文档版本，越大越新

    @property
    def weight(self) -> float:
        return SOURCE_WEIGHT.get(self.source, 0.2)

    def age_days(self, now: float) -> float:
        return max(0.0, (now - self.created_at) / DAY)

    def stale(self, now: float) -> bool:
        return self.age_days(now) > STALE_DAYS


# ------------------------------- 报告 -------------------------------


@dataclass
class HygieneReport:
    leaks_blocked: list[str] = field(default_factory=list)        # 跨用户
    stale_annotated: list[str] = field(default_factory=list)     # 过期标注
    low_authority_dropped: list[str] = field(default_factory=list)
    low_confidence_dropped: list[str] = field(default_factory=list)
    superseded_dropped: list[str] = field(default_factory=list)  # 被新版本取代
    conflicts: list[tuple[str, str, str]] = field(default_factory=list)  # (topic, 说法A, 说法B)
    kept: list[ContextItem] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "leaks_blocked": len(self.leaks_blocked),
            "stale_annotated": len(self.stale_annotated),
            "low_authority_dropped": len(self.low_authority_dropped),
            "low_confidence_dropped": len(self.low_confidence_dropped),
            "superseded_dropped": len(self.superseded_dropped),
            "conflicts": len(self.conflicts),
            "kept": len(self.kept),
        }

    @property
    def total_blocked(self) -> int:
        return (
            len(self.leaks_blocked)
            + len(self.low_authority_dropped)
            + len(self.low_confidence_dropped)
            + len(self.superseded_dropped)
        )


# ------------------------------- 组装 -------------------------------

_CLAIM_RE = re.compile(r"[0-9]+\s*(?:分钟|小时|天|秒)")


def _claim_of(text: str) -> str:
    """抽出"量化的说法"，用于判断两条描述是否矛盾。

    这里用"数字+单位"做代理：同一话题下"5 分钟"和"立即"就是两种说法。
    真实系统里应该用更结构化的表示（三元组或 JSON 字段）。
    """
    numbers = _CLAIM_RE.findall(text)
    return "|".join(numbers) if numbers else text.strip()


def assemble_naive(items: Iterable[ContextItem], *, now: float) -> str:
    """不做任何卫生处理：直接拼起来。

    这就是本节要复现的"污染现场"——多租户系统里最常见的那种。
    """
    return "\n".join(f"- {item.text}" for item in items)


ALL_FILTERS = frozenset({"namespace", "authority", "confidence", "version", "conflict", "stale"})


def assemble(
    items: Iterable[ContextItem],
    *,
    user_id: str,
    now: float,
    min_weight: float = MIN_WEIGHT,
    enabled: Iterable[str] = ALL_FILTERS,
) -> tuple[str, HygieneReport]:
    """带卫生处理的组装。四种解法**按顺序**叠加。

    `enabled` 用于演示"只开一个过滤器时它拦下了什么"。
    生产里不要传它——默认全开。
    """
    active = set(enabled)
    report = HygieneReport()
    survivors: list[ContextItem] = []

    # ① 命名空间隔离 + ② 来源权重 + 置信度
    for item in items:
        # 公共知识（policy/kb）user_id 为 "*"，不算泄漏；别人的私有内容才算
        if "namespace" in active and (
            item.source == "other_user" or item.user_id not in (user_id, SHARED_USER)
        ):
            report.leaks_blocked.append(f"{item.source}:{item.text[:16]}")
            continue
        if "authority" in active and item.weight < min_weight:
            report.low_authority_dropped.append(f"{item.source}({item.weight}):{item.text[:16]}")
            continue
        if "confidence" in active and item.confidence < MIN_CONFIDENCE:
            report.low_confidence_dropped.append(f"{item.source}({item.confidence}):{item.text[:16]}")
            continue
        survivors.append(item)

    # ③ 版本取代：同 topic 保留 version 最大的
    by_topic: dict[str, list[ContextItem]] = {}
    for item in survivors:
        if item.topic:
            by_topic.setdefault(item.topic, []).append(item)

    superseded_ids: set[str] = set()
    for topic, group in by_topic.items() if "version" in active else []:
        top = max(item.version for item in group)
        for item in group:
            if item.version < top:
                superseded_ids.add(item.id)
                report.superseded_dropped.append(f"{topic}@v{item.version}")
    survivors = [item for item in survivors if item.id not in superseded_ids]

    # ④ 冲突显式化
    for topic, group in by_topic.items() if "conflict" in active else []:
        live = [item for item in group if item.id not in superseded_ids]
        claims = {_claim_of(item.text) for item in live}
        if len(claims) > 1:
            first, second = live[0], live[1]
            report.conflicts.append((topic, first.text[:36], second.text[:36]))

    # ② 时效标注
    lines: list[str] = []
    for item in survivors:
        age = item.age_days(now)
        when = f"{age:.0f} 天前" if age >= 1 else "今天"
        tags = f"[{item.source}·{when}]"
        if "stale" in active and item.stale(now):
            tags = f"[{item.source}·{when}·**可能过期**]"
            report.stale_annotated.append(item.id)
        elif item.confidence < 0.8:
            tags = f"[{item.source}·{when}·置信度低]"
        lines.append(f"- {tags} {item.text}")

    if report.conflicts:
        lines.append("")
        lines.append("**注意：以下信息互相矛盾，请以最新政策为准并向用户确认：**")
        for topic, left, right in report.conflicts:
            lines.append(f"  - 关于「{topic}」：一种说法是「{left}」，另一种是「{right}」")

    report.kept = survivors
    return "\n".join(lines), report


# ------------------------------- 污染样例 -------------------------------


def build_polluted_items(now: float) -> list[ContextItem]:
    """造一批带污染的上下文：四种问题各一条以上。"""
    return [
        # 干净的政策
        ContextItem(
            id="p1", text="密码重置后 5 分钟内生效，期间请勿重复提交。",
            source="policy", user_id="*", created_at=now - 2 * DAY,
            topic="reset_policy", version=2, confidence=1.0,
        ),
        # 过期的旧政策（同 topic 但版本低）
        ContextItem(
            id="p0", text="密码重置后立即生效。",
            source="policy", user_id="*", created_at=now - 400 * DAY,
            topic="reset_policy", version=1, confidence=1.0,
        ),
        # 跨用户泄漏：别人的工单
        ContextItem(
            id="o1", text="李娜（E-3102）的工单：她的手机号 13800001234，VPN 已解封。",
            source="other_user", user_id="u-1001", created_at=now - DAY,
        ),
        # 上一轮的（错误）结论
        ContextItem(
            id="s1", text="上次结论：直接重置密码即可，无需确认身份。",
            source="session", user_id="u-1001", created_at=now - DAY,
            confidence=0.4,
        ),
        # 过期知识库条目
        ContextItem(
            id="k1", text="旧版客户端请使用 2024 版安装包。",
            source="kb", user_id="*", created_at=now - 500 * DAY,
        ),
        # 同 topic 但说法不同的知识库（冲突）
        ContextItem(
            id="k2", text="VPN 掉线后需要重新认证，通常会要求输入动态口令。",
            source="kb", user_id="*", created_at=now - 30 * DAY,
            topic="vpn_reauth",
        ),
        ContextItem(
            id="k3", text="VPN 掉线后会自动重连，不会要求重新认证。",
            source="kb", user_id="*", created_at=now - 10 * DAY,
            topic="vpn_reauth",
        ),
        # 来路不明的"偏方"（低权威来源）
        ContextItem(
            id="w1", text="网上帖子说：把 DNS 改成 8.8.8.8 就能解决 VPN 认证问题。",
            source="unknown", user_id="*", created_at=now - 5 * DAY,
        ),
    ]
