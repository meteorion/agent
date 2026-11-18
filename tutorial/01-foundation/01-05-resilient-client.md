---
section: "1.5"
title: "给 LLM 客户端装上超时、重试与异常分层"
stage: 1
status: done
depth: L2
difficulty: 3
minutes: 90
milestone: M1
depends_on: ["1.3"]
produces: ["llm/client.py"]
verifies: "断网与错 key 两种情形，分别看到「重试 3 次后报可读错误」和「立即失败」"
part: null
updated: "2026-11-17"
---

# 1.5 给 LLM 客户端装上超时、重试与异常分层

## 0. 本节导航

**本节在链路中的位置**：1.3 会发请求，1.4 会校验数据，但两者都是"一次就完"——成或死。本节加的是"**失败之后怎么办**"。它是 5.7（熔断降级）的地基，也是后面所有工具调用的统一错误出口。

**本节你将获得**

1. 一张**状态码 → 该不该重试**的决策表，并说清每个判断的理由。
2. 三层异常（`RetryableError` / `NeedsHumanError` / `FatalError`），让上层知道"该重试、该叫人、还是该改代码"。
3. 指数退避 + 抖动的完整实现，并实测"1000 个客户端不会同时醒来"。
4. 结构化日志：一条 `llm_retry` 日志里带 `attempt` / `delay_s` / `reason`，还能自动挂 `trace_id`。
5. 客户端账本：调用数、尝试数、重试数、token、费用，一次 `as_dict()` 全拿到。

**前置检查清单**

- 1.3 的 `uv run python -m scripts.llm_demo` 能跑通。
- 1.4 的 `uv run pytest` 是 28 passed。

**难度**：★★★☆☆ ｜ **预计时长**：90 分钟 ｜ **本节费用**：**¥0**（全程 mock 故障注入）

---

## 1. 为什么需要它（痛点先行）

1.3 的客户端是这么用的：

```python
result = await client.chat(messages)
```

**这一行只要失败一次，整个请求就失败。** 而线上 429（限流）和 503（过载）有多常见？任何跑过 LLM 生产系统的人都知道：高峰期每分钟都会遇到几次。

于是用户看到的是：

```
LLMError: HTTP 429: rate limit exceeded
```

然后点"重试"按钮。而他本来只需要多等 0.2 秒。

**第二个痛点：错误一视同仁。**

你的代码里大概有这么一个地方：

```python
try:
    result = await client.chat(messages)
except Exception:
    logger.error("LLM 调用失败，稍后重试")
    return "服务繁忙，请稍后再试"
```

于是这三种完全不同的情况，用户看到的是**同一句话**：

| 真实原因 | 该怎么做 | 用户实际看到 |
|---|---|---|
| 429 限流 | 等 0.2 秒重试，大概率成功 | "服务繁忙，请稍后再试" |
| 401 key 失效 | 立刻告警，运维换 key | "服务繁忙，请稍后再试" |
| 400 请求格式错 | 改代码，重试一万次也没用 | "服务繁忙，请稍后再试" |

**第三分钟就会被发现**——因为 401 的时候，你的监控上"LLM 错误率"飙到 100%，而你还在重试。

本节要解决的就是：**让每种错误都有人（或机器）知道该做什么。**

---

## 2. 核心概念与架构设计

### 2.1 类比：快递派送

你网购了一件货，快递员送不到。该怎么办？

| 情况 | 现实反应 | 对应错误 |
|---|---|---|
| 驿站今天关门了 | 明天再送一次（**值得重试**） | 5xx / 超时 |
| 今天件太多，排队 | 等一小时再来（**退避重试**） | 429 限流 |
| 收件人欠费被停机 | 快递员再跑十趟也没用，**得人去缴费** | 401 / 403 |
| 地址写错了 | 派送一百次都到不了，**得改单** | 400 / 422 |

> **这个类比在哪失效**：快递员可以打电话问你"是不是地址写错了"，而 HTTP 客户端不能——**它只能把"地址错了"这个判断交给你**。所以"分类"这件事必须写在代码里，没人会替你判断。

### 2.2 三层异常

```
AgentError（本项目所有可预期错误的基类）
├── RetryableError    暂时性故障，值得重试；抛出时代表「重试已用尽」
├── NeedsHumanError   重试无用，必须有人去改配置/换 key/开权限
└── FatalError        重试无用，是代码或 prompt 的问题
```

**为什么要分三层而不是两层（可重试 / 不可重试）？**

因为"不可重试"里藏着两种完全不同的处理：401 需要**半夜打电话叫醒人**，400 需要**明天改代码**。前者要告警、要值班响应；后者进缺陷列表就行。混在一起，你的告警会被 400 淹没，然后没人再看告警。

### 2.3 分类决策表

| 来源 | 判定 | 归类 | 理由 |
|---|---|---|---|
| HTTP 429 | 限流 | `RetryableError` | 等一会儿就好 |
| HTTP 500/502/503/504 | 服务端故障 | `RetryableError` | 同上 |
| HTTP 408 / 409 / 425 | 请求超时/冲突/过早 | `RetryableError` | 通常是瞬时的 |
| HTTP 401 / 403 | 认证/授权失败 | `NeedsHumanError` | 重试不会让 key 变有效 |
| HTTP 400 / 404 / 422 | 请求非法/资源不存在/语义错误 | `FatalError` | 重试只会重复犯错 |
| `httpx.TimeoutException` | 超时 | `RetryableError` | 网络抖动 |
| `httpx.NetworkError` | 连接失败/读失败 | `RetryableError` | 同上 |
| 其他异常 | 认不出 | **原样抛出** | 别把编程错误伪装成业务错误 |

**最后一行很重要**：`translate()` 对认不出的异常返回 `None`，调用方 `raise` 原始异常。把 `AttributeError` 包成 `FatalError`，会让你在排查时永远看不到真实堆栈。

### 2.4 退避曲线

```
delay
 2.0s ┤                    ┌───── 封顶 max_delay
 1.6s ┤           ┌────────┘
 0.8s ┤      ┌────┘
 0.4s ┤  ┌───┘
 0.2s ┤──┘
      └──┬────┬────┬────┬────► 重试次数
         1    2    3    4
      实际取值落在每条竖线的 [50%, 100%] 区间内（抖动）
```

### 2.5 分层结构（**本节的设计核心**）

```
LLMClient           ← 本节新加：重试、分类、日志、账本
    │  持有
    ▼
RawLLMClient        ← 1.3 的产物：只发一次请求，不懂重试
    │  持有
    ▼
httpx.AsyncClient   ← 连接池、超时
```

**为什么不把重试写进 `RawLLMClient`？**

因为"发一次"和"发到成功为止"是两件不同的事。混在一起后，你想测"单次请求的超时行为"就必须绕过重试逻辑；将来换 SDK，重试策略也得跟着重写。**分层之后，两层可以各自独立测试、独立替换。**

### 2.6 适用范围（边界）

| 场景 | 该重试吗 | 说明 |
|---|---|---|
| 查询类（问一句答一句） | ✅ 安全 | 重试不改变系统状态 |
| 生成类（写文件、发消息） | ⚠️ 需要幂等键 | 否则重试会重复执行 |
| 扣款、下单、删数据 | ❌ 除非有幂等保证 | 见 §8 坑 2 |

---

## 3. 环境与版本

```powershell
uv add structlog
```

实测：

```
 + structlog==26.1.0
```

| 组件 | 实测版本 | 说明 |
|---|---|---|
| structlog | `26.1.0` | 结构化日志 |
| httpx | `0.28.1` | 异常类型来源（`TimeoutException` / `NetworkError`） |
| Python | `3.12.13` | `except*` / `TaskGroup` 已在前几节用到 |

**验证装对了**

```powershell
uv run python -c "import structlog; print(structlog.__version__)"
```

```
26.1.0
```

---

## 4. 代码实战（6 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/errors.py` | 三层异常（全文内联） |
| `src/servicedesk/logging_setup.py` | structlog 配置 + trace 上下文（全文内联） |
| `src/servicedesk/llm/client.py` | **主角**：重试与分类（全文内联） |
| `scripts/mock_llm_server.py` | 新增故障注入（节选） |
| `scripts/resilience_demo.py` | 6 个场景 |
| `tests/test_client_resilience.py` | 8 个断言 |

### 4.1 `src/servicedesk/errors.py`

```python
# file: src/servicedesk/errors.py
"""异常分层：把"出错"拆成三种，让上层知道该做什么。

三种错误的处理方式完全不同：
- FatalError      → 别重试、别告警轰炸，这是代码/prompt 的问题，改完再发
- NeedsHumanError → 重试无用，但必须通知人（改配置、换 key、开权限）
- RetryableError  → 值得重试；重试用尽才抛出来

没有分层时，上层只能写 `except Exception: retry`，于是 401 被重试三次，
用户多等了 2 秒才看到"你的 key 无效"。
"""

from __future__ import annotations


class AgentError(Exception):
    """本项目所有「可预期错误」的基类。

    可预期 = 我们知道它会发生、知道怎么处理。
    不可预期（编程错误、内存溢出）不该被这个类捕获。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RetryableError(AgentError):
    """暂时性故障：429 限流、5xx、超时、网络抖动。

    抛出它意味着「已经重试过了，还是不行」。
    """

    def __init__(self, reason: str, *, attempts: int = 1) -> None:
        # base_reason 保留「不含尝试次数」的原始原因，日志里用它才不啰嗦
        self.base_reason = reason
        self.attempts = attempts
        super().__init__(f"{reason}（已尝试 {attempts} 次）")


class NeedsHumanError(AgentError):
    """需要人介入：key 无效、额度耗尽、权限不足。

    `hint` 是给运维看的那句话——写清楚"该去改什么"，
    否则半夜值班的人只能看到 401 然后开始猜。
    """

    def __init__(self, reason: str, *, hint: str = "") -> None:
        super().__init__(reason)
        self.hint = hint


class FatalError(AgentError):
    """重试没有意义的错误：请求格式错、字段缺失、资源不存在。

    这类错误 99% 是调用方代码或 prompt 的问题，重试只会重复犯错。
    """
```

### 4.2 `src/servicedesk/logging_setup.py`

```python
# file: src/servicedesk/logging_setup.py
"""结构化日志：让每条日志都带得上 trace_id 与关键字段。

为什么不用 print：
1. 线上要按 trace_id 把一次请求的所有日志串起来
2. 日志要能被机器解析（JSON），才能做"错误率""重试率"这类统计
3. 敏感字段（key、手机号）必须在写出去之前就被过滤
"""

from __future__ import annotations

import logging

import structlog


def configure_logging(*, level: str = "INFO", json_logs: bool = False) -> None:
    """进程启动时调一次。

    json_logs=False 时用彩色控制台格式（本地开发）；
    json_logs=True 时输出单行 JSON（生产，交给采集系统）。
    """
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
    )

    # httpx / httpcore 默认在 INFO 级别打「每一条请求」，
    # 开发时吵、生产时浪费存储。要排查就临时调到 DEBUG。
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            # 从 contextvars 里捞 trace_id 等上下文，自动挂到每条日志上
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def bind_request_context(**fields: object) -> None:
    """给当前请求绑定上下文，之后所有日志都会带上它。

    典型用法：
        bind_request_context(trace_id="abc123", user_id="u-9")
        ...
        clear_request_context()   # 请求结束务必清掉，否则会串到下一个请求
    """
    structlog.contextvars.bind_contextvars(**fields)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()


def get_logger(name: str = "servicedesk") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
```

### 4.3 `src/servicedesk/llm/client.py`

```python
# file: src/servicedesk/llm/client.py
"""生产级 LLM 客户端：重试、退避、异常分层、结构化日志、成本累计。

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

    async def chat(self, messages: list[Message], *, model: str | None = None) -> ChatResult:
        """发一次调用；失败时按分类决定重试还是直接抛。"""
        self.stats.calls += 1
        last_error: AgentError | None = None

        for attempt in range(1, self.max_attempts + 1):
            self.stats.attempts += 1
            try:
                result = await self._raw.chat(messages, model=model)
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
                return RetryableError(str(exc))   # str(LLMError) 已经带 HTTP 状态码
            if status in NEEDS_HUMAN_STATUS:
                return NeedsHumanError(str(exc), hint=_hint_for_status(status))
            return FatalError(str(exc))

        if isinstance(exc, httpx.TimeoutException):
            return RetryableError(f"超时: {type(exc).__name__}")
        if isinstance(exc, httpx.NetworkError):   # 含 ConnectError / ReadError 等
            return RetryableError(f"网络错误: {type(exc).__name__}: {exc}")
        return None
```

### 4.4 给 mock 加故障注入

测试重试必须有"按剧本出错"的服务端。在 `scripts/mock_llm_server.py` 里加了一个极小的故障队列：

```python
_FAULT_LOCK = threading.Lock()
_FAULTS: list[str] = []
_STATE: dict[str, int] = {"requests": 0}


def queue_faults(*codes: str) -> None:
    """排入故障剧本，按请求顺序消费。"""
    with _FAULT_LOCK:
        _FAULTS.extend(codes)


def _take_fault() -> str:
    with _FAULT_LOCK:
        return _FAULTS.pop(0) if _FAULTS else "ok"
```

`do_POST` 里消费它：

```python
        fault = _take_fault()
        if fault == "429":
            self._send_json(
                429,
                {"error": {"message": "rate limit exceeded", "type": "rate_limit_error"}},
                extra_headers={"Retry-After": "1"},
            )
            return
        if fault in ("500", "502"):
            self._send_json(int(fault), {"error": {"message": f"upstream error {fault}"}})
            return
        if fault == "timeout":
            time.sleep(0.5)
        if fault == "badjson":
            self._send_raw(200, b"<html>502 Bad Gateway</html>", "text/html")
            return
```

用法就一行：`queue_faults("429", "429")` 表示前两次 429、第三次正常。

### 4.5 `tests/test_client_resilience.py` 的关键设计

**测试不真的等待**——注入一个假的 `sleep`：

```python
class FakeSleep:
    """记录每次退避延迟，但不真的等。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
```

于是"重试用了几次、每次退避多久"都能精确断言，而整个文件只跑 **7 秒**（其中大头还是超时场景的真实等待）。

完整用例见 [tests/test_client_resilience.py](../../servicedesk-agent/tests/test_client_resilience.py)。

### 4.6 运行

```powershell
uv run python -m scripts.resilience_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么 `except Exception` 在这里是对的

```python
            except Exception as exc:  # noqa: BLE001
                error = self.translate(exc)
                if error is None:
                    raise
```

平时我们被教育"不要裸 `except Exception`"。但这里是**边界层**——它必须接住所有异常，然后做分类。关键是最后那个 `raise`：**认不出来的原样抛出**。

如果没有这两行，`AttributeError`、`KeyError` 这些编程错误会被吞成 `FatalError`，你的线上堆栈从此只剩一句"HTTP 400"。

### 5.2 为什么 401 不能被重试（实测数字）

把 401 也归为可重试，会发生什么？实测对比：

```
LLMClient  max_attempts=3: 用户等待 0.04s, 异常 NeedsHumanError, 尝试 1 次
BadClient  max_attempts=3: 用户等待 0.56s, 异常 RetryableError,   尝试 3 次
```

两个后果：

1. **用户多等 14 倍时间**（0.04s → 0.56s），拿到的却是同一句话。
2. **异常类型变成 `RetryableError`**，于是监控看到的是"暂时性故障，自动恢复中"，而真实情况是"key 挂了，全部请求都会失败"。**告警被降级了。**

### 5.3 退避为什么必须加抖动

```python
return capped * (0.5 + random.random() * 0.5)
```

实测（1000 次采样，`base_delay=0.2`，第 3 次重试）：

```
固定退避   : 全部 1.600s，唯一值 1 个
带抖动退避 : min=0.400s max=0.800s 唯一值 1000 个
抖动后标准差 0.1159s
```

**固定退避的灾难场景**：你的服务有 1000 个并发请求，某一刻 LLM 服务端抖了一下全部 503。1000 个客户端全都在 `t+1.6s` 这一秒醒来，同时打 1000 个请求——**服务端刚恢复就被你打垮了**。这就是 thundering herd。

加抖动后，重试分散在 `0.4s–0.8s` 的区间里，压力被摊平。

### 5.4 退避上限为什么是 2 秒而不是 30 秒

`max_delay_s=2.0` 是个产品决策，不是技术决策：

- 用户在前台等，`3 次 × 2s = 6s` 已经是忍耐极限。
- 后台批处理任务可以设到 30s。

**同一个客户端类，不同场景传不同参数**——这也是为什么 `max_delay_s` 是构造参数而不是常量。

### 5.5 `structlog` 的 `contextvars` 怎么用

```python
bind_request_context(trace_id="abc123", ticket_id="T-1001")
log.warning("llm_retry", attempt=2)
```

输出里会自动带上：

```
[warning] llm_retry  trace_id=abc123 ticket_id=T-1001 attempt=2
```

**代价**：`contextvars` 是**协程级共享**的。如果你在同一个任务里处理多个请求，或者忘了 `clear_request_context()`，`trace_id` 会串到下一个请求上——日志里就会出现"一个 trace_id 对应两个用户"的鬼故事。所以每个请求结束必须清。

### 5.6 为什么把 `sleep` 做成可注入的

```python
sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
```

这是一个很小的设计，但它决定了"重试逻辑能不能被测"。

如果写死 `await asyncio.sleep(delay)`，测试就必须真的等：3 次 500 的用例要等 `0.2+0.4=0.6s`，10 个用例就是 6 秒。而且**没法断言"退避延迟到底是多少"**——你只能断言"大概等了一会儿"。

注入之后，测试变成：

```python
assert 0.5 * exponential <= delay <= exponential
```

**可测性是设计出来的，不是测出来的。**

### 5.7 账本 `ClientStats` 的价值

```python
{'calls': 1, 'attempts': 3, 'retries': 2, 'prompt_tokens': 15, 'completion_tokens': 9, 'cost_cny': 1.5e-05}
```

这六个数字能回答四个生产问题：

| 问题 | 看哪个字段 |
|---|---|
| 我们的重试率高吗？（服务端稳不稳） | `retries / calls` |
| 一次用户请求平均打几次 HTTP？ | `attempts / calls` |
| 这个功能一天烧多少钱？ | `cost_cny` 累计 |
| 上下文是不是越滚越大？ | `prompt_tokens / calls` |

**没有账本的客户端，等于闭着眼睛开车。** 阶段五会把它接到真实的 metrics。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：去掉抖动，看 1000 个客户端如何同时醒来

把 `backoff()` 改成纯指数、无随机：

```python
    def backoff(self, attempt: int) -> float:
        return min(self.base_delay_s * (2 ** (attempt - 1)), self.max_delay_s)
```

实测第 3 次重试的 1000 次采样：

```
固定退避   : 全部 1.600s，唯一值 1 个
带抖动退避 : min=0.400s max=0.800s 唯一值 1000 个
```

**固定退避下，1000 个客户端的重试时刻是同一个瞬间。** 你以为是"自动恢复"，实际是"定时自杀"。

更隐蔽的问题：这种雪崩**只在故障恢复的那一刻发生**，平时压测根本测不出来。

### 6.2 实验 B：把 401 也当可重试

```python
class BadClient(LLMClient):
    def translate(self, exc):
        if isinstance(exc, LLMError) and exc.status_code in (401, 403):
            return RetryableError(str(exc))
        return super().translate(exc)
```

实测：

```
LLMClient  max_attempts=3: 用户等待 0.04s, 异常 NeedsHumanError, 尝试 1 次
BadClient  max_attempts=3: 用户等待 0.56s, 异常 RetryableError,   尝试 3 次
```

**注意第二个副作用比第一个严重得多**：异常类型从 `NeedsHumanError` 变成了 `RetryableError`。你的告警规则通常是"`NeedsHumanError` 立即 P1 电话，`RetryableError` 观察即可"，于是 key 全挂这种事，被降级成了"轻微抖动"。

---

## 7. 验证与预期输出

### 7.1 六个场景

```powershell
uv run python -m scripts.resilience_demo
```

实测输出：

```
mock 服务已启动: http://127.0.0.1:56735/v1

======================================================================
场景 1：429 两次后成功（重试救回来了）
======================================================================
  注入故障剧本: ('429', '429')
22:59:35 [warning  ] llm_retry     attempt=1 delay_s=0.045 next_attempt=2 reason='HTTP 429: rate limit exceeded'
22:59:35 [warning  ] llm_retry     attempt=2 delay_s=0.089 next_attempt=3 reason='HTTP 429: rate limit exceeded'
22:59:35 [info     ] llm_call_ok   attempts=3 completion_tokens=9 cost_cny=1.5e-05 model=mock-model prompt_tokens=15

  最终结果: ✅ 成功
  服务端实际收到请求: 3 次
  客户端账本: {'calls': 1, 'attempts': 3, 'retries': 2, ...}

======================================================================
场景 2：连续 500，重试用尽
======================================================================
  注入故障剧本: ('500', '500', '500')
22:59:35 [warning  ] llm_retry     attempt=1 delay_s=0.029 next_attempt=2 reason='HTTP 500: upstream error 500'
22:59:35 [warning  ] llm_retry     attempt=2 delay_s=0.086 next_attempt=3 reason='HTTP 500: upstream error 500'
22:59:35 [error    ] llm_call_exhausted attempt=3 reason='HTTP 500: upstream error 500'

  最终结果: RetryableError
  说明    : HTTP 500: upstream error 500（已尝试 3 次）
  服务端实际收到请求: 3 次

======================================================================
场景 3：没有 key（401）—— 不该重试
======================================================================
  最终结果: NeedsHumanError
  说明    : HTTP 401: 缺少 Authorization: Bearer <key>
  运维提示: 检查 LLM_API_KEY 是否有效、是否已过期或被吊销
  服务端实际收到请求: 1 次          ← 只打了一次
  客户端账本: {'calls': 1, 'attempts': 1, 'retries': 0, ...}

======================================================================
场景 4：messages 顺序错（400）—— 不该重试
======================================================================
  最终结果: FatalError
  说明    : HTTP 400: messages[1] 是 tool 消息，但前一条不是带 tool_calls 的 assistant 消息 —— 顺序错了
  服务端实际收到请求: 1 次

======================================================================
场景 5：读超时（每次 0.1s 就放弃，服务端要 0.5s）
======================================================================
  最终结果: RetryableError
  说明    : 超时: ReadTimeout（已尝试 3 次）
  服务端实际收到请求: 3 次

======================================================================
场景 6：端口根本连不上（网络类错误）
======================================================================
  最终结果: RetryableError
  说明    : 网络错误: ConnectError: All connection attempts failed（已尝试 2 次）
```

### 7.2 测试

```powershell
uv run pytest
```

```
....................................                                     [100%]
36 passed in 7.27s
```

（1.1 的 3 + 1.2 的 3 + 1.3 的 9 + 1.4 的 13 + 本节的 8。）

---

## 8. 生产环境踩坑实录

### 坑 1：把 4xx 全当可重试，故障时流量放大 3 倍

**现象**

某天上游返回 400（我们的请求体多了个字段），重试率 100%，QPS 瞬间变成 3 倍，把限流配额打穿。真正的故障是"请求格式错了"，但表现出来的是"限流"。

**定位方法**

看 `retries / attempts` 这个比值：

```python
print(client.stats.as_dict())   # retries 接近 attempts 就是重试风暴
```

再看日志里 `llm_retry` 的 `reason` 是不是集中在同一个错误。

**根因**

`except Exception: retry`，或者把 `4xx` 整体归为可重试。**4xx 是"你的问题"，重试不会修好你的问题。**

**修法**

```diff
- if 400 <= status < 500:
-     return RetryableError(...)
+ if status in RETRYABLE_STATUS:      # 白名单，不是黑名单
+     return RetryableError(...)
+ if status in NEEDS_HUMAN_STATUS:
+     return NeedsHumanError(...)
+ return FatalError(...)
```

**用白名单而不是黑名单**：新出现的状态码（比如某个网关自创的 4xx）默认落入 `FatalError`，不会意外引发重试风暴。

### 坑 2：重试导致副作用执行两次

**现象**

用户投诉"我的工单被创建了三条"。查日志发现同一个 `trace_id` 下有三个 `create_ticket` 成功记录。

**定位方法**

在工具执行处打点，看同一个业务请求有没有重复执行：

```python
log.info("tool_executed", tool="create_ticket", idempotency_key=key)
```

**根因**

LLM 调用本身是幂等的（问同一个问题不会改变系统状态），但**它调用的工具往往不是**。重试一次 `chat()`，可能会让模型把"创建工单"这个工具调用再发一次。

**修法**

给所有写操作加**幂等键**，并让工具执行层去重：

```python
async def create_ticket(payload: Ticket, *, idempotency_key: str) -> str:
    cached = await idem_store.get(idempotency_key)
    if cached:
        return cached                 # 之前已经成功过，直接返回同一个结果
    ticket_id = await db.insert(payload)
    await idem_store.set(idempotency_key, ticket_id, ttl=86400)
    return ticket_id
```

幂等键的生成规则：`hash(trace_id + tool_name + 参数摘要)`。阶段三做 LangGraph 时，这个键会变成 checkpoint 的一部分。

**顺手加固**：只对"读操作"允许自动重试；写操作要么幂等，要么明确不重试。

### 坑 3：重试把原始异常吞了，线上只剩一句人话

**现象**

线上报 `FatalError: HTTP 400: ...`，但不知道在代码的哪一行触发的，`__traceback__` 指向的是 `raise error`，不是真正的出错点。

**定位方法**

```python
import traceback
traceback.print_exception(type(exc), exc, exc.__traceback__)
```

如果堆栈最底下是 `client.py` 的 `raise error`，说明异常链断了。

**根因**

`raise error` 而不是 `raise error from exc`。后者会把原始异常挂在 `__cause__` 上，堆栈能一路看到底。

**修法**

```diff
- raise error
+ raise error from exc
```

**顺手加固**：自定义异常类里保存 `str(exc)` 只够"给人看"，要保留机器可分析的信息就得保留原对象：

```python
class RetryableError(AgentError):
    def __init__(self, reason: str, *, attempts: int = 1, cause: Exception | None = None):
        super().__init__(reason)
        self.cause = cause
```

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么 401 是 <code>NeedsHumanError</code> 而不是 <code>FatalError</code>？</b></summary>

`FatalError` 的语义是"代码或 prompt 有问题，改完再发"；401 是"配置有问题，人要去改"。两者的响应链路完全不同：前者进缺陷列表，后者要打电话。混成一类，你的 P1 告警会被真正的代码 bug 淹没。

</details>

<details>
<summary><b>Q2：退避为什么要"有界抖动"而不是"完全随机"？</b></summary>

完全随机（`random.uniform(0, max)`）会让部分客户端在 0.01 秒后就重试，等于没退避；有界抖动（`[50%, 100%] × 指数值`）既保留了"越退越久"的趋势，又打散了同时刻的洪峰。实测 1000 个样本得到 1000 个不同取值，标准差 0.1159s。

</details>

<details>
<summary><b>Q3：把 <code>sleep</code> 做成可注入参数，除了测试还有什么好处？</b></summary>

可以按场景换策略：后台批处理用"长退避 + 无限重试"，前台交互用"短退避 + 3 次放弃"；也可以在压测时替换成"立即返回"来观察重试风暴的上限。**依赖注入的本质不是为测试，是为可变性。**

</details>

### 9.2 动手任务：支持 `Retry-After`，并加上"重试预算"

**要求**

1. 让 `LLMError` 携带 `retry_after: float | None`（从响应头 `Retry-After` 解析），`translate()` 把它传进 `RetryableError`。
2. `backoff()` 改成：**如果服务端给了 `Retry-After`，就用它**（并仍然加上 ±20% 的抖动），否则走指数退避。
3. 新增"重试预算"：`LLMClient` 接受 `retry_budget_s: float = 10.0`，如果累计等待已超过预算，**立即放弃**并抛 `RetryableError`。
4. 补测试：`Retry-After: 5` 且预算 10s 时，第二次重试后就该放弃。

**验收标准**

- mock 的 429 已经返回 `Retry-After: 1`，用它验证第 2 条。
- `retry_budget_s=0.5` 时，`max_attempts=5` 的客户端最多尝试 2–3 次就放弃（用 `FakeSleep` 累加验证）。
- 日志里能看到 `retry_after_s` 字段。
- 新增 2 个 pytest 用例，全绿。

**提示**：真实世界里 `Retry-After` 可能是秒数，也可能是 HTTP 日期（`Wed, 21 Oct 2015 07:28:00 GMT`），两种都要处理。这也是最容易被忽略的一个兼容性问题。

### 9.3 拓展挑战

给 `LLMClient` 加一个"熔断"：连续 N 次 `RetryableError` 后，**直接短路**接下来 M 秒内的所有调用，抛出 `NeedsHumanError("服务已熔断")`。

**思考**：熔断器打开后，恢复探测怎么做（半开状态）？如果所有客户端实例各自维护一个熔断器，会不会出现"每个实例都熔断、每个实例都恢复探测"的震荡？这时候状态该放哪？

---

## 10. 本节小结与延伸

**三点回顾**

1. **错误要分三类，不是两类**：可重试、要叫人、要改代码。混在一起，告警必然失效（实测 401 若被重试，异常会从 `NeedsHumanError` 降级成 `RetryableError`）。
2. **重试必须带抖动**：固定退避下 1000 个客户端的重试时刻是同一个瞬间（实测唯一值 1 个 vs 1000 个），这是"故障恢复时二次雪崩"的标准配方。
3. **可测性来自设计**：把 `sleep` 注入进去，8 个重试用例才能在 7 秒内跑完，并且能精确断言"退避延迟是多少"。

**已知限制（诚实记录）**

- **未支持 `Retry-After`**：服务端说"等 1 秒"，我们目前按自己的指数算。留作 §9.2 的作业。
- **未做重试预算**：极端情况下总等待时间可能超过用户忍耐上限。
- **未做熔断**：持续故障时仍会每个请求重试 3 次。5.7 会正经实现。

**延伸阅读**

- AWS 关于指数退避与抖动的经典说明：https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
- httpx 异常层次（`TimeoutException` / `NetworkError` 的继承关系）：https://www.python-httpx.org/exceptions/
- structlog 上下文绑定：https://www.structlog.org/en/stable/contextvars.html
- HTTP 状态码规范（哪些是暂时性）：https://developer.mozilla.org/en-US/docs/Web/HTTP/Status

---

## 修订记录

- 2026-11-17 初稿：6 个场景与 8 个测试全部实机运行，§7 为真实输出
- 2026-11-17 修正：`RetryableError` 增加 `base_reason`，解决日志里出现「（已尝试 1 次）（已尝试 3 次）」的重复拼接；`translate()` 不再重复加 `HTTP {status}:` 前缀
- 2026-11-17 修正：`configure_logging` 静音 httpx/httpcore 的逐请求日志；mock 的响应写入捕获 `ConnectionError`，消除客户端超时中断时的 stderr 堆栈
- 2026-11-17 补充：§6 两个实验为实测数据（抖动分布 1000 个唯一值；401 重试使等待从 0.04s 涨到 0.56s 且异常类型降级）
