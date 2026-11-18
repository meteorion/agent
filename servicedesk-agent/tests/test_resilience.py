# file: tests/test_resilience.py
"""5.7 的断言：限流、熔断三态、成本闸、降级链、以及四道闸的串联顺序。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from servicedesk.resilience.resilience import (
    BreakerState,
    CircuitBreaker,
    CostGate,
    FallbackTier,
    RateLimiter,
    ResilienceManager,
    TokenBucket,
)


@dataclass
class FakeClock:
    t: float = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ------------------------------ 令牌桶 ------------------------------


def test_bucket_allows_up_to_capacity() -> None:
    clock = FakeClock()
    bucket = TokenBucket(capacity=3, refill_per_s=1, clock=clock)
    assert [bucket.try_take() for _ in range(4)] == [True, True, True, False]


def test_bucket_refills_over_time() -> None:
    clock = FakeClock()
    bucket = TokenBucket(capacity=3, refill_per_s=1, clock=clock)
    for _ in range(3):
        bucket.try_take()

    clock.advance(2)
    assert bucket.try_take() is True
    assert bucket.try_take() is True
    assert bucket.try_take() is False


def test_bucket_does_not_exceed_capacity() -> None:
    clock = FakeClock()
    bucket = TokenBucket(capacity=2, refill_per_s=1, clock=clock)
    clock.advance(100)
    assert bucket.available == pytest.approx(2.0)


# ------------------------------ 限流 ------------------------------


def test_rate_limiter_is_per_key() -> None:
    clock = FakeClock()
    limiter = RateLimiter(capacity=2, refill_per_s=1, clock=clock)

    assert limiter.allow("u-1") and limiter.allow("u-1")
    assert limiter.allow("u-1") is False, "u-1 的桶已空"

    assert limiter.allow("u-2") is True, "别的用户不受影响"
    assert limiter.as_dict()["rejected"] == {"u-1": 1}


# ------------------------------ 熔断 ------------------------------


def test_breaker_opens_after_threshold() -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=5, clock=FakeClock())
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN


def test_open_breaker_rejects_without_calling() -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=5, clock=FakeClock())
    breaker.record_failure()
    assert breaker.allow() is False
    assert breaker.allow() is False
    assert breaker.rejected == 2


def test_breaker_half_opens_after_cooldown() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=5, clock=clock)
    breaker.record_failure()

    clock.advance(4)
    assert breaker.allow() is False

    clock.advance(1.1)
    assert breaker.allow() is True
    assert breaker.state is BreakerState.HALF_OPEN


def test_half_open_limits_probes() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=5, half_open_max_calls=1, clock=clock)
    breaker.record_failure()
    clock.advance(6)

    assert breaker.allow() is True, "第一个探测放行"
    assert breaker.allow() is False, "探测中不再放行"


def test_half_open_success_closes() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=5, clock=clock)
    breaker.record_failure()
    clock.advance(6)
    breaker.allow()

    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.failures == 0


def test_half_open_failure_reopens_and_resets_cooldown() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=5, clock=clock)
    breaker.record_failure()
    clock.advance(6)
    breaker.allow()

    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    clock.advance(4)
    assert breaker.allow() is False, "重新计冷却，4 秒还不够"


# ------------------------------ 成本闸 ------------------------------


def test_cost_gate_allows_within_budget() -> None:
    gate = CostGate(daily_budget=100, clock=FakeClock())
    assert gate.can_spend(40) is True
    gate.record(40)
    assert gate.remaining() == 60
    assert gate.can_spend(60) is True
    assert gate.can_spend(61) is False


def test_cost_gate_resets_next_day() -> None:
    clock = FakeClock(t=0.0)
    gate = CostGate(daily_budget=100, clock=clock)
    gate.record(100)
    assert gate.can_spend(1) is False

    clock.advance(86_400)
    assert gate.remaining() == 100, "跨天后预算重置"


def test_cost_gate_counts_rejections() -> None:
    gate = CostGate(daily_budget=10, clock=FakeClock())
    gate.record(10)
    gate.can_spend(5)
    gate.can_spend(5)
    assert gate.as_dict()["rejected"] == 2


# ------------------------------ 降级与串联 ------------------------------


def make_manager(clock: FakeClock, *, fallbacks: list[FallbackTier] | None = None) -> ResilienceManager:
    async def cache() -> str:
        return "缓存答复"

    async def template() -> str:
        return "模板答复"

    return ResilienceManager(
        limiter=RateLimiter(capacity=10, refill_per_s=1, clock=clock),
        cost_gate=CostGate(daily_budget=1000, clock=clock),
        breaker=CircuitBreaker(failure_threshold=2, cooldown_s=5, clock=clock),
        fallbacks=fallbacks if fallbacks is not None else [
            FallbackTier("cache", cache), FallbackTier("template", template)
        ],
    )


async def test_manager_primary_success() -> None:
    manager = make_manager(FakeClock())

    async def primary() -> str:
        return "正常"

    result = await manager.call(primary, estimated_tokens=10)
    assert result.ok and result.tier == "primary"
    assert manager.cost_gate is not None and manager.cost_gate.used == 10


async def test_manager_rate_limited_uses_fallback() -> None:
    clock = FakeClock()
    manager = make_manager(clock)
    manager.limiter = RateLimiter(capacity=2, refill_per_s=1, clock=clock)

    async def primary() -> str:
        return "正常"

    await manager.call(primary)
    await manager.call(primary)
    result = await manager.call(primary)      # 第三次超限
    assert result.ok is False
    assert result.tier == "cache"
    assert "rate_limited" in result.reason


async def test_manager_cost_limited_uses_fallback() -> None:
    clock = FakeClock()
    manager = make_manager(clock)
    manager.cost_gate = CostGate(daily_budget=10, clock=clock)

    async def primary() -> str:
        return "正常"

    result = await manager.call(primary, estimated_tokens=100)
    assert result.tier == "cache"
    assert "cost_limited" in result.reason


async def test_manager_error_opens_breaker_then_circuit_open() -> None:
    clock = FakeClock()
    manager = make_manager(clock)

    async def boom() -> str:
        raise RuntimeError("下游 500")

    await manager.call(boom)
    await manager.call(boom)
    result = await manager.call(boom)

    assert result.tier == "cache"
    assert "circuit_open" in result.reason
    assert manager.breaker.state is BreakerState.OPEN


async def test_manager_without_fallbacks_returns_apology() -> None:
    manager = make_manager(FakeClock(), fallbacks=[])

    async def boom() -> str:
        raise RuntimeError("炸了")

    result = await manager.call(boom)
    assert result.ok is False
    assert result.tier == "none"
    assert "稍后重试" in result.text


async def test_concurrency_only_hits_downstream_until_breaker_opens() -> None:
    """验收点：并发 10 个请求，下游实际只被调用 3 次（熔断阈值）。"""
    clock = FakeClock()
    calls = {"n": 0}

    async def primary() -> str:
        calls["n"] += 1
        raise RuntimeError("下游 500")

    async def cache() -> str:
        return "缓存答复"

    manager = ResilienceManager(
        limiter=RateLimiter(capacity=5, refill_per_s=1, clock=clock),
        cost_gate=CostGate(daily_budget=1000, clock=clock),
        breaker=CircuitBreaker(failure_threshold=3, cooldown_s=5, clock=clock),
        fallbacks=[FallbackTier("cache", cache)],
    )

    results = await asyncio.gather(
        *[manager.call(primary, key="u-1", estimated_tokens=10) for _ in range(10)]
    )

    assert calls["n"] == 3, f"下游被打了 {calls['n']} 次"
    assert manager.breaker.state is BreakerState.OPEN
    assert all(result.tier == "cache" for result in results)
    assert len(results) == 10, "所有请求都拿到了答复（降级）"

    clock.advance(6)
    assert manager.breaker.allow() is True, "冷却后自动进入 HALF_OPEN"
