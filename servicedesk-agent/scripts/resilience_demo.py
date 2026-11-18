# file: scripts/resilience_demo.py
"""5.7 的场景：限流、熔断、降级、成本闸，在真实并发下跑一遍。

用法：
    uv run python -m scripts.resilience_demo
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from servicedesk.resilience.resilience import (
    BreakerState,
    CircuitBreaker,
    CostGate,
    FallbackTier,
    RateLimiter,
    ResilienceManager,
    TokenBucket,
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


@dataclass
class FakeClock:
    """可推进的假时钟——熔断/限流都要"等时间"，测试不能真等。"""

    t: float = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ------------------------------- 演示 -------------------------------


def demo_bucket() -> None:
    header("第 1 步：令牌桶 —— 平滑限流，不是固定窗口")
    clock = FakeClock()
    bucket = TokenBucket(capacity=5, refill_per_s=1, clock=clock)

    print(f"  容量 5，每秒补 1 个令牌。初始可用: {bucket.available:.1f}")
    results = []
    for index in range(8):
        ok = bucket.try_take()
        results.append("✅" if ok else "❌")
        print(f"    第 {index + 1} 次: {'通过' if ok else '拒绝'}   剩余 {bucket.available:.1f}")

    print()
    print("  等 3 秒后：")
    clock.advance(3)
    for index in range(4):
        ok = bucket.try_take()
        results.append("✅" if ok else "❌")
        print(f"    第 {index + 9} 次: {'通过' if ok else '拒绝'}   剩余 {bucket.available:.1f}")

    print()
    print("  >>> 为什么不用固定窗口：59.9 秒放 100 个 + 60.1 秒放 100 个 =")
    print("      **2 秒内 200 个**。令牌桶是匀速恢复的，不会在边界翻倍。")


def demo_rate_limit_concurrency() -> None:
    header("第 2 步：限流 + 并发 —— 10 个请求打进来")
    clock = FakeClock()

    bucket_limiter = RateLimiter(capacity=5, refill_per_s=1, clock=clock)
    passed = [bucket_limiter.allow("u-1") for _ in range(10)]
    print(f"  并发 10 个（容量 5）: 通过 {sum(passed)}，拒绝 {10 - sum(passed)}")
    print(f"  按 key 统计: {bucket_limiter.as_dict()['rejected']}")

    clock.advance(2)
    passed2 = [bucket_limiter.allow("u-1") for _ in range(4)]
    print(f"  等 2 秒后再来 4 个: 通过 {sum(passed2)}（补了 2 个令牌）")
    print()
    print("  >>> 限流要**按 key 分桶**：一个用户刷爆不该影响别人。")


async def demo_breaker() -> None:
    header("第 3 步：熔断 —— 连续失败后不再发起调用")
    clock = FakeClock()
    downstream = {"calls": 0, "should_fail": True}

    async def primary() -> str:
        downstream["calls"] += 1
        if downstream["should_fail"]:
            raise RuntimeError("下游 500")
        return "正常答复"

    breaker = CircuitBreaker(name="llm", failure_threshold=3, cooldown_s=5, clock=clock)

    print("  连续调用（下游一直失败）：")
    for index in range(6):
        if not breaker.allow():
            print(f"    第 {index + 1} 次: ⛔ 熔断中，**不发起调用**")
            continue
        try:
            await primary()
        except RuntimeError:
            breaker.record_failure()
            print(f"    第 {index + 1} 次: ❌ 调用失败（累计失败 {breaker.failures}）")

    print(f"\n  下游实际被调用: {downstream['calls']} 次（6 个请求只打了 3 次）")
    print(f"  熔断状态: {breaker.state.value}")

    print()
    print("  冷却 5 秒后（HALF_OPEN 探测）：")
    clock.advance(5)
    print(f"    状态自动变为: {breaker.state.value if breaker.allow() else 'open'}")
    downstream["should_fail"] = False
    await primary()
    breaker.record_success()
    print(f"    探测成功 → 状态: {breaker.state.value}")

    print()
    print("  另一种情况：探测**失败**")
    breaker2 = CircuitBreaker(failure_threshold=1, cooldown_s=5, clock=clock)
    breaker2.record_failure()
    clock.advance(6)
    breaker2.allow()
    print(f"    进 HALF_OPEN: {breaker2.state.value}")
    breaker2.record_failure()
    print(f"    探测失败 → 回到: {breaker2.state.value}（重新计冷却）")
    print()
    print("  >>> 熔断不是「失败后重试」，是「失败太多次后**不再发起调用**」。")
    print("      6 个请求只打了 3 次下游——把资源让给还活着的路径。")


async def demo_fallback() -> None:
    header("第 4 步：降级链 —— 主路径挂了给什么")
    clock = FakeClock()
    mode = {"primary": "fail", "cache": "ok", "template": "ok"}

    async def primary() -> str:
        if mode["primary"] == "fail":
            raise RuntimeError("模型超时")
        return "模型生成的答复"

    async def cache() -> str:
        if mode["cache"] != "ok":
            raise RuntimeError("缓存也没命中")
        return "上一次的缓存答复"

    async def template() -> str:
        return "通用排查步骤：请先重启客户端、检查网络。"

    manager = ResilienceManager(
        breaker=CircuitBreaker(failure_threshold=5, cooldown_s=5, clock=clock),
        fallbacks=[FallbackTier("cache", cache), FallbackTier("template", template)],
    )

    scenarios = [
        ("一切正常", {"primary": "ok"}),
        ("模型挂了，缓存命中", {"primary": "fail", "cache": "ok"}),
        ("模型挂了，缓存也没", {"primary": "fail", "cache": "miss"}),
    ]
    for label, state in scenarios:
        mode.update({"primary": "fail", "cache": "ok"})
        mode.update(state)
        result = await manager.call(primary)
        print(f"  {pad(label, 22)}{pad(result.tier, 12)}{result.text[:34]}")

    print()
    print("  >>> 降级不是「返回错误」，是「**返回一个还行的东西**」。")
    print("      顺序：缓存（最像原答案）→ 模板（兜底）→ 人工（最后手段）。")


async def demo_cost_gate() -> None:
    header("第 5 步：成本闸 —— 预算烧完了怎么办")
    clock = FakeClock()
    gate = CostGate(daily_budget=1000, clock=clock)

    print(f"  每日预算 1000 token。每次调用估 300。")
    for index in range(5):
        allowed = gate.can_spend(300)
        if allowed:
            gate.record(300)
        print(f"    第 {index + 1} 次: {'✅ 放行' if allowed else '⛔ 拒绝'}"
              f"   剩余 {gate.remaining()}")

    print()
    print(f"  统计: {gate.as_dict()}")
    print()
    print("  >>> 成本闸的价值：**在账单爆炸之前拦住**，而不是月底看账单才发现。")
    print("      拒绝时应该降级（小模型/缓存），而不是直接报错。")


async def demo_manager() -> None:
    header("第 6 步：四道闸串起来 —— 顺序有讲究")
    clock = FakeClock()
    downstream = {"calls": 0, "fail": True}

    async def primary() -> str:
        downstream["calls"] += 1
        if downstream["fail"]:
            raise RuntimeError("下游 500")
        return "正常答复"

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
    tiers: dict[str, int] = {}
    for result in results:
        tiers[result.tier] = tiers.get(result.tier, 0) + 1

    print(f"  并发 10 个请求（容量 5，失败阈值 3）：")
    print(f"    降级分布: {tiers}")
    print(f"    下游实际被调用: {downstream['calls']} 次")
    print(f"    熔断状态: {manager.breaker.state.value}")
    print(f"    限流拒绝: {manager.as_dict()['limiter']['rejected']}")

    print()
    print("  顺序为什么是 限流 → 成本 → 熔断：")
    rows = [
        ("① 限流", "最便宜，一个内存判断", "拦住刷量，保护所有下游"),
        ("② 成本", "次便宜，一次加法", "拦住烧钱，保护钱包"),
        ("③ 熔断", "要看下游历史", "拦住已经坏掉的调用"),
        ("④ 调主路径", "最贵", "只有前三关都过了才花钱"),
    ]
    print(f"  {pad('顺序', 14)}{pad('成本', 26)}作用")
    print("  " + "-" * 74)
    for name, cost, effect in rows:
        print(f"  {pad(name, 14)}{pad(cost, 26)}{effect}")
    print()
    print("  >>> 把贵的判断放前面，等于白花钱。**便宜的闸要在前面。**")


async def main() -> int:
    demo_bucket()
    demo_rate_limit_concurrency()
    await demo_breaker()
    await demo_fallback()
    await demo_cost_gate()
    await demo_manager()

    header("小结")
    print("  1. 令牌桶平滑限流；固定窗口会在边界放过 2 倍流量。")
    print("  2. 熔断三态：连续失败→OPEN（不发起调用）→冷却→HALF_OPEN 探测→成功回 CLOSED。")
    print("  3. 降级是「返回一个还行的东西」，不是「返回错误」。")
    print("  4. 四道闸顺序：限流 → 成本 → 熔断 → 调用；便宜的在前。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
