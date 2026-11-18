# file: tests/test_client_resilience.py
"""1.5 的断言：什么该重试、什么不该、退避是否受控。

测试不真的等待——往 LLMClient 注入一个假的 sleep，
既能验证"退避延迟是多少"，又能让整个文件跑在毫秒级。
"""

from __future__ import annotations

import pytest

from scripts.mock_llm_server import clear_faults, queue_faults, request_count, start_mock_server
from servicedesk.config import Settings
from servicedesk.errors import FatalError, NeedsHumanError, RetryableError
from servicedesk.llm.client import LLMClient
from servicedesk.logging_setup import configure_logging

configure_logging(level="CRITICAL")   # 测试里不要日志噪音

USER = [{"role": "user", "content": "总结一下今天的工单"}]


@pytest.fixture(scope="module")
def base_url():
    server, url = start_mock_server()
    yield url
    server.shutdown()


@pytest.fixture(autouse=True)
def _clean_faults():
    clear_faults()
    yield
    clear_faults()


def count_delta(before: int) -> int:
    """只数"这次调用新增了几次请求"。

    为什么不用绝对值：mock server 的计数器是**模块级全局**的，
    多个模块的 server 同时活着时，晚到的请求线程会把它加上去（偶发 flake）。
    断言增量既保留原意（这次只发了 N 次），又不受其它模块干扰。
    """
    return request_count() - before


def make_settings(
    base_url: str,
    *,
    api_key: str | None = "mock-key",
    timeout_s: float = 5.0,
) -> Settings:
    return Settings(
        _env_file=None,
        llm_base_url=base_url,
        llm_model="mock-model",
        llm_api_key=api_key,
        request_timeout_s=timeout_s,
    )


class FakeSleep:
    """记录每次退避延迟，但不真的等。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def build(base_url: str, sleeper: FakeSleep, **kwargs: object) -> LLMClient:
    return LLMClient(
        make_settings(base_url),
        base_delay_s=0.2,
        max_delay_s=1.0,
        sleep=sleeper,
        **kwargs,  # type: ignore[arg-type]
    )


# ------------------------------ 可重试 ------------------------------


async def test_retries_on_429_then_succeeds(base_url: str) -> None:
    queue_faults("429", "429")           # 第三次默认 ok
    before = request_count()
    sleeper = FakeSleep()
    client = build(base_url, sleeper)

    async with client:
        result = await client.chat(USER)

    assert result.text
    assert client.stats.calls == 1
    assert client.stats.attempts == 3
    assert client.stats.retries == 2
    assert count_delta(before) == 3
    assert len(sleeper.delays) == 2, "每次重试都应有一次退避"


async def test_backoff_grows_and_is_capped(base_url: str) -> None:
    """第 n 次退避应在 [0.5, 1.0] × (base × 2^(n-1)) 之间，且不超过 max_delay。"""
    before = request_count()
    sleeper = FakeSleep()
    client = build(base_url, sleeper)

    for attempt in (1, 2, 3, 4):
        delay = client.backoff(attempt)
        exponential = min(0.2 * (2 ** (attempt - 1)), 1.0)
        assert 0.5 * exponential <= delay <= exponential
        assert delay <= 1.0


async def test_server_error_is_retried_then_exhausted(base_url: str) -> None:
    queue_faults("500", "500", "500")
    before = request_count()
    sleeper = FakeSleep()
    client = build(base_url, sleeper)

    async with client:
        with pytest.raises(RetryableError) as excinfo:
            await client.chat(USER)

    assert excinfo.value.attempts == 3
    assert count_delta(before) == 3, "最多尝试 max_attempts 次，不能无限重试"


async def test_timeout_is_retryable(base_url: str) -> None:
    queue_faults("timeout", "timeout")     # 两次都要超时，否则第二次就成功了
    before = request_count()
    sleeper = FakeSleep()
    settings = make_settings(base_url, timeout_s=0.1)
    client = LLMClient(settings, max_attempts=2, base_delay_s=0.01, sleep=sleeper)

    async with client:
        with pytest.raises(RetryableError):
            await client.chat(USER)

    assert count_delta(before) == 2


async def test_connection_refused_is_retryable() -> None:
    before = request_count()
    sleeper = FakeSleep()
    settings = make_settings("http://127.0.0.1:1/v1")
    client = LLMClient(settings, max_attempts=2, base_delay_s=0.01, sleep=sleeper)

    async with client:
        with pytest.raises(RetryableError):
            await client.chat(USER)


# ----------------------------- 不可重试 -----------------------------


async def test_missing_key_is_needs_human_and_not_retried(base_url: str) -> None:
    before = request_count()
    sleeper = FakeSleep()
    settings = make_settings(base_url, api_key=None)
    client = LLMClient(settings, max_attempts=3, sleep=sleeper)

    async with client:
        with pytest.raises(NeedsHumanError) as excinfo:
            await client.chat(USER)

    assert "LLM_API_KEY" in excinfo.value.hint
    assert count_delta(before) == 1, "401 重试三次只会让用户多等两秒"
    assert client.stats.retries == 0


async def test_bad_request_is_fatal_and_not_retried(base_url: str) -> None:
    bad = [
        {"role": "user", "content": "查工单 T-1001"},
        {"role": "tool", "content": "处理中", "tool_call_id": "call_1"},
    ]
    before = request_count()
    sleeper = FakeSleep()
    client = build(base_url, sleeper)

    async with client:
        with pytest.raises(FatalError):
            await client.chat(bad)

    assert count_delta(before) == 1
    assert client.stats.retries == 0


# ------------------------------ 账本 ------------------------------


async def test_stats_accumulate_tokens_and_cost(base_url: str) -> None:
    before = request_count()
    sleeper = FakeSleep()
    client = build(base_url, sleeper)

    async with client:
        first = await client.chat(USER)
        second = await client.chat(USER)

    assert client.stats.calls == 2
    assert client.stats.attempts == 2
    assert client.stats.prompt_tokens == first.usage.prompt_tokens + second.usage.prompt_tokens
    assert client.stats.cost_cny == pytest.approx(first.cost_cny + second.cost_cny)
