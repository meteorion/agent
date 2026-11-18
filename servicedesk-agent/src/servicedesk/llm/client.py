# file: src/servicedesk/llm/client.py
"""在生产级 LLM 客户端：重试、退避、异常分层、结构化日志、成本累计。

分层设计（重要）：
    RawLLMClient   —— 只负责「发一次请求」，不懂重试（1.3 的产物）
    LLMClient      —— 负责「这次调用最终成功还是失败」，含重试与归类

这样拆的好处：单个客户端的职责单一，重试策略可独立测试、独立调整，
将来换 SDK 只需要替换 RawLLMClient。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from servicedesk.config import Settings
from servicedesk.errors import AgentError, FatalError, NeedsHumanError, RetryableError
from servicedesk.llm.raw_client import (
    ChatResult,
    LLMError,
    Message,
    RawLLMClient,
    estimate_cost,
)
from servicedesk.logging_setup import get_logger

log = get_logger("llm.client")


# ------------------------------- 统计信息 -------------------------------


@dataclass
class ClientStats:
    """一次运行内的累计账本。生产里应该把它接到 metrics。"""

    calls: int = 0            # 逻辑调用次数（一次 chat() 算一次）
    attempts: int = 0         # 实际 HTTP 尝试次数（含重试）
    retries: int = 0          # 重试次数
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "calls": self.calls,
            "attempts": self.attempts,
            "retries": self.retries,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_cny": round(self.cost_cny, 6),
        }


# ------------------------------- 错误分类 -------------------------------

# 哪些 HTTP 状态码值得再试一次
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
# 哪些是"人要去改配置"的
NEEDS_HUMAN_STATUS = {401, 403}


def _hint_for_status(status: int) -> str:
    if status == 401:
        return "检查 LLM_API_KEY 是否有效、是否已过期或被吊销"
    if status == 403:
        return "检查该 key 是否有目标模型的调用权限，或组织是否已开通"
    return ""


# -------------------------------- 客户端 --------------------------------


@dataclass
class LLMClient:
    """带重试与异常分层的客户端。

    用法：
        async with LLMClient() as client:
            result = await client.chat([{"role": "user", "content": "你好"}])
    """

    settings: Settings | None = None
    max_attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0

    # 可注入的 sleep，测试里换成假的就不用真的等
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    _raw: RawLLMClient = field(init=False)
    stats: ClientStats = field(default_factory=ClientStats, init=False)

    def __post_init__(self) -> None:
        self._raw = RawLLMClient(self.settings)

    # -- 生命周期 --

    async def __aenter__(self) -> LLMClient:
        await self._raw.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._raw.__aexit__(*exc_info)

    # -- 对外方法 --

    async def chat(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> ChatResult:
        """发一次调用；失败时按分类决定重试还是直接抛。"""
        self.stats.calls += 1
        last_error: AgentError | None = None

        for attempt in range(1, self.max_attempts + 1):
            self.stats.attempts += 1
            try:
                result = await self._raw.chat(
                    messages, model=model, tools=tools, tool_choice=tool_choice
                )
            except Exception as exc:  # noqa: BLE001 —— 这里必须兜住，再分类
                error = self.translate(exc)
                if error is None:
                    raise  # 认不出来的错误不要吞，原样抛给开发者

                last_error = error
                if not isinstance(error, RetryableError):
                    log.error(
                        "llm_call_rejected",
                        attempt=attempt,
                        error_type=type(error).__name__,
                        reason=error.reason,
                        hint=getattr(error, "hint", ""),
                    )
                    raise error from exc

                if attempt >= self.max_attempts:
                    log.error(
                        "llm_call_exhausted",
                        attempt=attempt,
                        reason=error.base_reason,
                    )
                    break

                delay = self.backoff(attempt)
                self.stats.retries += 1
                log.warning(
                    "llm_retry",
                    attempt=attempt,
                    next_attempt=attempt + 1,
                    delay_s=round(delay, 3),
                    reason=error.base_reason,
                )
                await self.sleep(delay)
                continue

            self.record_usage(result)
            log.info(
                "llm_call_ok",
                attempts=attempt,
                model=result.model,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cost_cny=round(result.cost_cny, 6),
            )
            return result

        # 重试用尽：把最后一次的错误升级成"RetryableError(attempts=N)"
        assert last_error is not None
        if isinstance(last_error, RetryableError):
            # 用 base_reason，否则会拼出「（已尝试 1 次）（已尝试 3 次）」
            raise RetryableError(last_error.base_reason, attempts=self.max_attempts)
        raise last_error

    # -- 内部 --

    def record_usage(self, result: ChatResult) -> None:
        self.stats.prompt_tokens += result.usage.prompt_tokens
        self.stats.completion_tokens += result.usage.completion_tokens
        self.stats.cost_cny += result.cost_cny

    def backoff(self, attempt: int) -> float:
        """指数退避 + 有界抖动。

        attempt=1 → base * 1，attempt=2 → base * 2，以此类推，封顶 max_delay。
        抖动取 [50%, 100%] 的随机比例：避免大量客户端同时重试把服务再打垮
        （这叫 thundering herd，同步重试是线上雪崩的经典原因）。
        """
        exponential = self.base_delay_s * (2 ** (attempt - 1))
        capped = min(exponential, self.max_delay_s)
        return capped * (0.5 + random.random() * 0.5)

    def translate(self, exc: Exception) -> AgentError | None:
        """把底层异常翻译成三层之一；认不出来返回 None。"""
        if isinstance(exc, LLMError):
            status = exc.status_code or 0
            if status in RETRYABLE_STATUS:
                return RetryableError(str(exc))   # str(LLMError) 里已经带了 HTTP 状态码
            if status in NEEDS_HUMAN_STATUS:
                return NeedsHumanError(str(exc), hint=_hint_for_status(status))
            return FatalError(str(exc))

        if isinstance(exc, httpx.TimeoutException):
            return RetryableError(f"超时: {type(exc).__name__}")
        if isinstance(exc, httpx.NetworkError):   # 含 ConnectError / ReadError 等
            return RetryableError(f"网络错误: {type(exc).__name__}: {exc}")
        return None
