# file: tests/test_raw_client.py
"""1.3 的断言：客户端对着 mock 服务的行为必须与真实服务一致。

这些用例不联网、不需要 API Key，可直接进 CI。
"""

from __future__ import annotations

import httpx
import pytest

from scripts.mock_llm_server import REPLY_TEXT, start_mock_server
from servicedesk.config import Settings
from servicedesk.llm.raw_client import LLMError, RawLLMClient, Usage, estimate_cost


@pytest.fixture(scope="module")
def base_url():
    """整个模块共用一个 mock 服务，省去反复起停的开销。"""
    server, url = start_mock_server()
    yield url
    server.shutdown()


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


USER = [{"role": "user", "content": "异步到底解决什么问题？"}]


# ------------------------------- 纯函数 -------------------------------


def test_estimate_cost_is_linear_in_tokens() -> None:
    """100 万输入 + 100 万输出，按 mock-model 单价 (0.30, 1.20) = 1.50 元。"""
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert estimate_cost(usage, "mock-model") == pytest.approx(1.50)


def test_unknown_model_falls_back_to_default_pricing() -> None:
    usage = Usage(prompt_tokens=1_000_000)
    assert estimate_cost(usage, "some-unlisted-model") == pytest.approx(1.00)


# ------------------------------- 非流式 -------------------------------


async def test_chat_returns_text_usage_and_cost(base_url: str) -> None:
    async with RawLLMClient(make_settings(base_url)) as client:
        result = await client.chat(USER)

    assert result.text == REPLY_TEXT
    assert result.usage.total_tokens > 0
    assert result.cost_cny > 0
    assert result.model == "mock-model"


async def test_chat_without_key_raises_401(base_url: str) -> None:
    async with RawLLMClient(make_settings(base_url, api_key=None)) as client:
        with pytest.raises(LLMError) as excinfo:
            await client.chat(USER)

    assert excinfo.value.status_code == 401
    assert "Bearer" in str(excinfo.value)


async def test_tool_message_without_tool_calls_raises_400(base_url: str) -> None:
    """顺序违规必须是 400，而不是静默返回答案。"""
    bad = [
        {"role": "user", "content": "查工单 T-1001"},
        {"role": "tool", "content": "处理中", "tool_call_id": "call_1"},
    ]
    async with RawLLMClient(make_settings(base_url)) as client:
        with pytest.raises(LLMError) as excinfo:
            await client.chat(bad)

    assert excinfo.value.status_code == 400


# -------------------------------- 流式 --------------------------------


async def test_stream_collects_deltas_and_final_usage(base_url: str) -> None:
    deltas: list[str] = []
    usage = None

    async with RawLLMClient(make_settings(base_url)) as client:
        async for chunk in client.stream(USER):
            if chunk.delta:
                deltas.append(chunk.delta)
            if chunk.usage is not None:
                usage = chunk.usage

    assert "".join(deltas) == REPLY_TEXT
    assert len(deltas) > 1, "流式应该分多块返回，而不是一次性给完"
    assert usage is not None and usage.total_tokens > 0


async def test_stream_without_include_usage_has_no_usage(base_url: str) -> None:
    usage = None
    async with RawLLMClient(make_settings(base_url)) as client:
        async for chunk in client.stream(USER, include_usage=False):
            if chunk.usage is not None:
                usage = chunk.usage

    assert usage is None, "没要 include_usage 就不该拿到 usage"


# -------------------------------- 超时 --------------------------------


async def test_read_timeout_is_raised(base_url: str) -> None:
    """服务端 1s 才回，客户端只等 0.3s，必须抛 ReadTimeout。"""
    slow = [{"role": "user", "content": "#slow 慢请求"}]
    async with RawLLMClient(make_settings(base_url, timeout_s=0.3)) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.chat(slow)


# ------------------------------- 生命周期 -------------------------------


async def test_client_raises_when_used_outside_context_manager() -> None:
    client = RawLLMClient(make_settings("http://127.0.0.1:1/v1"))
    with pytest.raises(LLMError):
        await client.chat(USER)
