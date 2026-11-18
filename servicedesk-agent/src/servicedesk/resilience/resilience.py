# file: src/servicedesk/resilience/resilience.py
"""韧性：熔断、限流、成本闸、降级。

四件事，各自挡不同的东西：

    限流（RateLimiter）   —— 挡"太多"：同一用户/租户请求过密
    成本闸（CostGate）     —— 挡"太贵"：一天的 token 预算烧完了
    熔断（CircuitBreaker） —— 挡"已经坏了"：下游连续失败时别再打
    降级（Fallback）       —— 挡"全挂"：主路径不可用时给什么

它们的关系是**串联**：限流 → 成本 → 熔断 → 调用 → 降级。

⚠️ 一个常见误解：以为熔断是"失败后重试"。**不是**——熔断是"失败太多次之后
**不再发起调用**"，把资源让给还活着的路径。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable

Clock = Callable[[], float]


def _default_clock() -> float:
    return time.monotonic()


# ------------------------------- 熔断 -------------------------------


class BreakerState(str, Enum):
    CLOSED = "closed"          # 正常通行
    OPEN = "open"              # 拒绝一切（快速失败）
    HALF_OPEN = "half_open"    # 放少量探测请求


@dataclass
class CircuitBreaker:
    """三态熔断器。

        CLOSED ──(连续失败 >= threshold)──► OPEN
          ▲                                   │
          │                            (冷却时间到)
          └──(探测成功)── HALF_OPEN ◄─────────┘
                            │
                       (探测失败) → 回 OPEN 并重新计时

    **关键设计**：OPEN 期间 `allow()` 返回 False，**不发起任何调用**——
    这样下游还有机会恢复，而不是被持续打穿。
    """

    name: str = "default"
    failure_threshold: int = 3
    cooldown_s: float = 5.0
    half_open_max_calls: int = 1
    clock: Clock = _default_clock

    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    opened_at: float = 0.0
    half_open_inflight: int = 0
    rejected: int = 0
    succeeded: int = 0

    def _refresh(self) -> None:
        """按冷却时间推进状态：OPEN 到点了就进 HALF_OPEN。"""
        if self.state is BreakerState.OPEN and self.clock() - self.opened_at >= self.cooldown_s:
            self.state = BreakerState.HALF_OPEN
            self.half_open_inflight = 0
            self.failures = 0

    def allow(self) -> bool:
        self._refresh()
        if self.state is BreakerState.CLOSED:
            return True
        if self.state is BreakerState.OPEN:
            self.rejected += 1
            return False
        # HALF_OPEN：只放少量探测
        if self.half_open_inflight < self.half_open_max_calls:
            self.half_open_inflight += 1
            return True
        self.rejected += 1
        return False

    def record_success(self) -> None:
        self.succeeded += 1
        self.failures = 0
        self.state = BreakerState.CLOSED
        self.half_open_inflight = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.state is BreakerState.HALF_OPEN:
            # 探测失败：回到 OPEN，重新冷却
            self.state = BreakerState.OPEN
            self.opened_at = self.clock()
            self.half_open_inflight = 0
            return
        if self.failures >= self.failure_threshold:
            self.state = BreakerState.OPEN
            self.opened_at = self.clock()

    def as_dict(self) -> dict[str, Any]:
        self._refresh()
        return {
            "name": self.name,
            "state": self.state.value,
            "failures": self.failures,
            "rejected": self.rejected,
            "succeeded": self.succeeded,
        }


# ------------------------------- 限流 -------------------------------


@dataclass
class TokenBucket:
    """令牌桶：容量 + 匀速补充。

    **为什么不用固定窗口**：固定窗口在边界会放过 2 倍流量
    （59.9 秒 100 个 + 60.1 秒 100 个 = 2 秒 200 个）。令牌桶是平滑的。
    """

    capacity: float = 10.0
    refill_per_s: float = 1.0
    clock: Clock = _default_clock
    tokens: float = field(init=False)
    last_refill: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        self.last_refill = self.clock()

    def _refill(self) -> None:
        now = self.clock()
        elapsed = max(0.0, now - self.last_refill)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_s)
        self.last_refill = now

    def try_take(self, amount: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    @property
    def available(self) -> float:
        self._refill()
        return self.tokens


class RateLimiter:
    """按 key（用户/租户）分桶的限流器。"""

    def __init__(
        self, *, capacity: float = 10.0, refill_per_s: float = 1.0, clock: Clock = _default_clock
    ) -> None:
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self.clock = clock
        self._buckets: dict[str, TokenBucket] = {}
        self.rejected: dict[str, int] = {}

    def bucket(self, key: str) -> TokenBucket:
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(
                capacity=self.capacity, refill_per_s=self.refill_per_s, clock=self.clock
            )
        return self._buckets[key]

    def allow(self, key: str, amount: float = 1.0) -> bool:
        if self.bucket(key).try_take(amount):
            return True
        self.rejected[key] = self.rejected.get(key, 0) + 1
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "buckets": {key: round(bucket.available, 2) for key, bucket in self._buckets.items()},
            "rejected": dict(sorted(self.rejected.items())),
        }


# ------------------------------- 成本闸 -------------------------------


class CostGate:
    """token 预算闸。按"天"重置（真实系统里按租户/项目分账）。"""

    def __init__(self, *, daily_budget: int = 100_000, clock: Clock = _default_clock) -> None:
        self.daily_budget = daily_budget
        self.clock = clock
        self.used = 0
        self._day = self._today()
        self.rejected = 0

    def _today(self) -> int:
        # 用 wall clock 的天数决定"哪天"，但用注入的 clock 便于测试
        return int(self.clock() // 86_400)

    def _rollover(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self.used = 0

    def remaining(self) -> int:
        self._rollover()
        return max(0, self.daily_budget - self.used)

    def can_spend(self, estimated: int) -> bool:
        self._rollover()
        if estimated <= self.remaining():
            return True
        self.rejected += 1
        return False

    def record(self, tokens: int) -> None:
        self._rollover()
        self.used += max(0, tokens)

    def as_dict(self) -> dict[str, Any]:
        return {
            "budget": self.daily_budget,
            "used": self.used,
            "remaining": self.remaining(),
            "rejected": self.rejected,
        }


# ------------------------------- 降级 -------------------------------


@dataclass
class FallbackTier:
    """降级的一档。`kind` 只用于观测与话术。"""

    kind: str
    handler: Callable[[], Awaitable[str]]


@dataclass
class CallResult:
    ok: bool
    text: str
    tier: str                 # primary / cache / template / human / rate_limited …
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "tier": self.tier, "reason": self.reason, "text": self.text[:60]}


# ------------------------------- 管理器 -------------------------------


class ResilienceManager:
    """把四道闸串起来。**顺序很重要**（§5.1）。"""

    def __init__(
        self,
        *,
        limiter: RateLimiter | None = None,
        cost_gate: CostGate | None = None,
        breaker: CircuitBreaker | None = None,
        fallbacks: Iterable[FallbackTier] = (),
    ) -> None:
        self.limiter = limiter
        self.cost_gate = cost_gate
        self.breaker = breaker or CircuitBreaker()
        self.fallbacks = list(fallbacks)
        self.tier_counts: dict[str, int] = {}

    async def call(
        self,
        primary: Callable[[], Awaitable[str]],
        *,
        key: str = "default",
        estimated_tokens: int = 0,
    ) -> CallResult:
        # ① 限流：最先挡，因为最便宜
        if self.limiter is not None and not self.limiter.allow(key):
            return await self._fallback("rate_limited", "请求过于频繁")

        # ② 成本闸：第二便宜
        if self.cost_gate is not None and not self.cost_gate.can_spend(estimated_tokens):
            return await self._fallback("cost_limited", "今日预算已用完")

        # ③ 熔断：此时才知道要不要真的发起调用
        if not self.breaker.allow():
            return await self._fallback("circuit_open", "下游连续失败，已熔断")

        try:
            text = await primary()
        except Exception as exc:  # noqa: BLE001
            self.breaker.record_failure()
            return await self._fallback("error", f"{type(exc).__name__}: {exc}")

        self.breaker.record_success()
        if self.cost_gate is not None:
            self.cost_gate.record(estimated_tokens)
        self._count("primary")
        return CallResult(ok=True, text=text, tier="primary")

    async def _fallback(self, reason: str, detail: str) -> CallResult:
        for tier in self.fallbacks:
            try:
                text = await tier.handler()
            except Exception:  # noqa: BLE001
                continue
            self._count(tier.kind)
            return CallResult(ok=False, text=text, tier=tier.kind, reason=f"{reason}: {detail}")
        self._count("none")
        return CallResult(ok=False, text="服务暂时不可用，请稍后重试。", tier="none",
                          reason=f"{reason}: {detail}")

    def _count(self, tier: str) -> None:
        self.tier_counts[tier] = self.tier_counts.get(tier, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier_counts": dict(sorted(self.tier_counts.items())),
            "breaker": self.breaker.as_dict(),
            "limiter": self.limiter.as_dict() if self.limiter else None,
            "cost": self.cost_gate.as_dict() if self.cost_gate else None,
        }


def make_default_manager(
    *,
    daily_budget: int = 100_000,
    rate_capacity: float = 10.0,
    rate_refill: float = 1.0,
    clock: Clock = _default_clock,
) -> ResilienceManager:
    """一套常用配置：限流 10 并发/1 QPS + 每日预算 + 3 次失败熔断。"""
    return ResilienceManager(
        limiter=RateLimiter(capacity=rate_capacity, refill_per_s=rate_refill, clock=clock),
        cost_gate=CostGate(daily_budget=daily_budget, clock=clock),
        breaker=CircuitBreaker(failure_threshold=3, cooldown_s=5.0, clock=clock),
    )
