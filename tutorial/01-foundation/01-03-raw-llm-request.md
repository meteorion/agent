---
section: "1.3"
title: "不借 SDK，亲手发一次 LLM 请求"
stage: 1
status: done
depth: L2
difficulty: 3
minutes: 90
milestone: M1
depends_on: ["1.2"]
produces: ["src/servicedesk/llm/raw_client.py"]
verifies: "能贴出流式输出，并算出单次调用真实费用"
part: null
updated: "2026-11-17"
---

# 1.3 不借 SDK，亲手发一次 LLM 请求

> **验证范围说明**：本节全链路已对**本地 mock 服务**实测通过（6 个场景 + 9 个测试）。
> 真实 API 走的是同一份代码，切换只需改 `.env` 两项——但那段没跑过，因为本机没有 Key。
> 见 §10「已知未验证项」。

## 0. 本节导航

**本节在链路中的位置**：1.2 讲清了"异步是在等的时候不闲着"，本节就真正去"等"一次——**手写**一次 LLM 请求。后面 1.4 要把它的输出变成结构化数据，1.5 要在它外面套上重试与超时，2.1 要用它的 `tool_calls` 字段做函数调用。**这一节是所有 LLM 交互的地基。**

**本节你将获得**

1. 亲眼看到"调用大模型"就是一次普通 `POST /chat/completions`，没有魔法。
2. 能说清 `system` / `user` / `assistant` / `tool` 四种角色的语义与**顺序约束**。
3. 用 SSE 逐块解析流式输出，实测"首字延迟 0.007s / 总耗时 0.134s"。
4. 用 `usage` 算出单次调用的真实费用，并能解释为什么流式调用常常**算不出**费用。
5. 配好四段超时与连接池，让慢请求拖不住进程。

**前置检查清单**

- 1.1 的 `uv run python -m servicedesk` 能打出 `✓ 环境自检通过`。
- 1.2 的 `uv run pytest` 是 6 passed。

**难度**：★★★☆☆ ｜ **预计时长**：90 分钟 ｜ **本节费用**：**¥0**（全程对本地 mock 发请求；真调一遍约 ¥0.00002 量级，见 §5.5）

---

## 1. 为什么需要它（痛点先行）

几乎所有教程的第一步都是 `pip install openai`，然后：

```python
from openai import OpenAI
client = OpenAI()
print(client.chat.completions.create(model="...", messages=[...]).choices[0].message.content)
```

三行跑通，然后你就卡住了。因为一旦出错，你看到的是：

```
openai.APIConnectionError: Connection error.
```

**到底哪一步错了？** 是 key 不对？base_url 少了 `/v1`？代理没配？被防火墙拦了？还是 DNS 挂了？

SDK 把这五种完全不同的故障，压成了同一句话。你开始猜、开始换 key、开始重装库——这就是"会用 SDK 但不会写系统"的分界线。

再看第二个痛点：**流式输出的体感**。

非流式调用，用户要盯着转圈等到整段答案生成完（3–10 秒）。流式调用，用户 0.1 秒就能看到第一个字。这两者的产品体验差异，比"模型强 5%"重要得多。

但如果你用的是"假流式"——先 `await` 整个响应，再一段段打印——**体感一点没变，只是代码更复杂了**。实测数据在 §6.1：真流式首字 `0.006s`，假流式首字 `0.138s`，差距 23 倍。

**所以这一节要做两件事**：把 LLM 调用扒到 HTTP 层看清本质；把流式做成真的。

---

## 2. 核心概念与架构设计

### 2.1 类比：一台自动售货机

调用 LLM 就像用自动售货机：

- **地址**（`base_url`）= 售货机装在哪
- **投币口**（HTTP `POST`）= 你只能投币，不能掀开机器自己拿
- **按键编号**（`payload` 里的 `model` / `messages`）= 你要哪一排哪一格
- **掉出来的货**（响应 JSON 的 `choices[0].message.content`）= 你的答案
- **小票**（响应里的 `usage`）= 告诉你这次花了多少（这里是 token）

> **这个类比在哪失效**：售货机不会"答错"，它要么给货要么退币。LLM 会**自信地胡说**，而且不收费也不报错。所以后面（1.8、5.2）才需要 RAG 和评估——那不是本节的事，但你现在就该知道"拿到了 200 响应"不等于"拿到了正确答案"。

### 2.2 一次调用到底发了什么

```
你的代码
   │
   │  POST /v1/chat/completions
   │  Authorization: Bearer sk-...
   │  Content-Type: application/json
   ▼
┌──────────────────────────────┐
│ {                            │
│   "model": "gpt-4.1-mini",   │
│   "messages": [              │
│     {"role":"system", ...},  │
│     {"role":"user",   ...}   │
│   ],                         │
│   "stream": false            │
│ }                            │
└──────────────────────────────┘
   │
   ▼
┌──────────────────────────────┐
│ 200 OK                       │
│ {                            │
│   "choices": [{"message":    │
│     {"content": "..."}}],    │
│   "usage": {"prompt_tokens": │
│     14, "completion_tokens": │
│     9, "total_tokens": 23}   │
│ }                            │
└──────────────────────────────┘
```

**请求体就是一个 JSON 字典，响应体也是一个 JSON 字典。** SDK 做的全部事情，就是拼这个字典、发出去、解析回来，再包一层异常类。

### 2.3 流式（SSE）长什么样

`stream: true` 时，响应不再是"一次性给完的 JSON"，而是 `text/event-stream`：

```
data: {"choices":[{"delta":{"content":"异步"}}]}

data: {"choices":[{"delta":{"content":"不是让"}}]}

data: {"choices":[{"delta":{"content":"计算更快"}}]}

data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{...}}

data: [DONE]
```

三个必须记住的格式规则：

1. **每行以 `data: ` 开头**（`data:` 后可能有一个空格，也可能没有）。
2. **事件之间用空行分隔**，空行本身不是数据。
3. **以 `data: [DONE]` 结束**，它不是 JSON，直接 `json.loads` 会崩。

### 2.4 四种角色的语义（以及顺序铁律）

| 角色 | 谁写的 | 作用 | 常见错误 |
|---|---|---|---|
| `system` | 你（开发者） | 设定身份、规则、输出格式 | 写在 `user` 后面，被忽略 |
| `user` | 终端用户 | 提需求 | 把系统指令混进来（提示注入） |
| `assistant` | 模型 | 上一轮的回答，或要调用工具的请求 | 手动伪造，导致上下文错乱 |
| `tool` | 你的代码 | 把工具执行结果回喂给模型 | **没有紧跟在带 `tool_calls` 的 assistant 之后** |

**顺序铁律**：`tool` 消息必须紧跟在一条**带 `tool_calls` 的 `assistant` 消息**之后。这就像对话：模型先说"我要查一下工单"（assistant + tool_calls），你才能回"查到了，状态是处理中"（tool）。跳步就是病句。

实测（§7 场景 5）：

```
HTTP 400: messages[1] 是 tool 消息，但前一条不是带 tool_calls 的 assistant 消息 —— 顺序错了
```

这条约束在第 2.1 节做 Function Calling 时会天天遇到，现在先记住。

### 2.5 逐层拆解：五个部件

| 部件 | 作用 | 漏了会怎样 |
|---|---|---|
| `base_url` | 服务地址（含 `/v1`） | 404，且报错信息通常很含糊 |
| `Authorization` | `Bearer <key>` | 401 |
| `messages` | 对话内容与角色顺序 | 400 |
| `stream` | 是否流式 | 无报错，但体感差 23 倍 |
| `stream_options.include_usage` | 流式时是否要 usage | 无报错，但**永远算不出花了多少钱** |

### 2.6 适用范围（边界）

| 场景 | 用手写 HTTP | 用官方 SDK |
|---|---|---|
| 学习、排障、要看清请求体 | ✅ | ❌ |
| 接的是"OpenAI 兼容"的第三方/自建服务 | ✅ 最省事 | ⚠️ 可能要改 base_url + 各种兼容性坑 |
| 要用音频、文件、批处理等新特性 | ❌ 追不动 | ✅ |
| 团队协作、长期维护 | ⚠️ 要自己维护异常层 | ✅ |

**本教程的选择**：1.3–2.x 用手写（你要看清工具调用协议的每一层），5.x 会换回官方 SDK 做对照。

---

## 3. 环境与版本

### 3.1 新增一个依赖

```powershell
cd servicedesk-agent
uv add httpx
```

实测：

```
 + httpx==0.28.1
 + httpcore==1.0.9
 + h11==0.16.0
 + certifi==2026.10.23
 + idna==3.20
```

### 3.2 版本组合

| 组件 | 实测版本 | 说明 |
|---|---|---|
| httpx | `0.28.1` | `AsyncClient.stream()` / `aiter_lines()` 用它 |
| Python | `3.12.13` | `asyncio.timeout` 需要 ≥3.11 |
| pydantic | `2.14.3` | `Usage` 模型用它 |

### 3.3 配置（两种走法）

**走法 A：本地 mock（默认，零成本，无需 Key）**

不用改 `.env`，`scripts/llm_demo.py` 会自己起一个 mock 服务并把 `base_url` 指过去。

**走法 B：真实服务**

在 `.env` 里填上：

```ini
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4.1-mini
LLM_API_KEY=sk-你的真实key
```

然后 `uv run python scripts/llm_demo.py` 换成你自己的脚本即可——**客户端代码一行都不用改**。

### 3.4 验证装对了

```powershell
uv run python -c "import httpx; print(httpx.__version__)"
```

```
0.28.1
```

---

## 4. 代码实战（5 个文件）

| 文件 | 角色 | 是否内联 |
|---|---|---|
| `scripts/mock_llm_server.py` | 本地假服务（约 220 行） | 只展示契约与 `_stream` 核心 |
| `src/servicedesk/llm/__init__.py` | 包标记 | 一行 |
| `src/servicedesk/llm/raw_client.py` | **本节主角**：客户端 | 全文 |
| `scripts/llm_demo.py` | 6 个场景的演示 | 摘要 + 链接 |
| `tests/test_raw_client.py` | 验收依据 | 全文 |

### 4.1 `scripts/mock_llm_server.py`（为什么需要它）

没有 Key 也要能练。这个 mock 实现了与真实服务**一致**的三个行为：

| 请求 | 行为 |
|---|---|
| 无 `Authorization: Bearer` | `401` + `{"error":{"message":"缺少 Authorization: Bearer <key>"}}` |
| `messages` 里 `tool` 不跟在带 `tool_calls` 的 `assistant` 后 | `400` + 具体错误 |
| `stream: true` | `text/event-stream`，逐块 `data: {...}`，最后 `data: [DONE]` |
| `stream_options.include_usage` 未设置 | 最后一个 chunk **不带** usage（与真实服务一致） |
| 用户消息含 `#slow` | 延迟 1 秒再响应（用来测超时） |

核心的流式实现：

```python
    def _stream(self, payload: dict[str, Any]) -> None:
        model = payload.get("model", "mock-model")
        include_usage = bool((payload.get("stream_options") or {}).get("include_usage"))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        try:
            # 逐块下发 3 个字符，模拟真实模型"边生成边吐"
            for start in range(0, len(REPLY_TEXT), 3):
                piece = REPLY_TEXT[start : start + 3]
                self._write_event({..., "choices": [{"index": 0, "delta": {"content": piece}}]})
                time.sleep(0.02)

            final = {..., "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            # 关键细节：只有显式要求 include_usage，usage 才会出现。
            if include_usage:
                final["usage"] = _usage(payload)
            self._write_event(final)

            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except ConnectionError:
            # 客户端中途断开（比如它自己的超时到了）——安静收尾
            return
```

完整实现见 [scripts/mock_llm_server.py](../../servicedesk-agent/scripts/mock_llm_server.py)。

### 4.2 `src/servicedesk/llm/raw_client.py`（全文）

```python
# file: src/servicedesk/llm/raw_client.py
"""不借任何 SDK，直接用 httpx 调 chat/completions。

为什么先自己写一遍：
1. 你会看清 LLM 调用**本质就是一次 HTTP POST**，没有魔法。
2. 出问题时你知道该看请求体还是响应体，而不是对着 SDK 的黑盒猜。
3. 后面用官方 SDK 时，你知道它替你做了什么、代价是什么。

对着 scripts/mock_llm_server.py 可全链路跑通；把 LLM_BASE_URL 换成真实
地址即可无缝切换。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import NotRequired, TypedDict

import httpx
from pydantic import BaseModel

from servicedesk.config import Settings, get_settings


# ------------------------------- 数据契约 -------------------------------


class Message(TypedDict):
    """一条对话消息。role 的取值含义见 §2.4。"""

    role: str          # system | user | assistant | tool
    content: str
    tool_call_id: NotRequired[str]


class Usage(BaseModel):
    """token 用量。真实服务在响应体的 usage 字段里给。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class Pricing:
    """单价：元 / 百万 token。"""

    input_per_million: float
    output_per_million: float


# ⚠️ 示例价，不是报价单。上线前必须按所用模型的最新官网价目改写。
PRICING: dict[str, Pricing] = {
    "gpt-4.1-mini": Pricing(0.30, 1.20),
    "mock-model": Pricing(0.30, 1.20),
}
DEFAULT_PRICING = Pricing(1.00, 3.00)


def estimate_cost(usage: Usage, model: str) -> float:
    """按 token 数估算这一次调用的花费（元）。"""
    price = PRICING.get(model, DEFAULT_PRICING)
    return (
        usage.prompt_tokens * price.input_per_million
        + usage.completion_tokens * price.output_per_million
    ) / 1_000_000


@dataclass
class ChatResult:
    text: str
    usage: Usage
    cost_cny: float
    model: str


@dataclass
class StreamChunk:
    """流式响应的一块。usage 只在最后一个块里出现。"""

    delta: str = ""
    usage: Usage | None = None


class LLMError(RuntimeError):
    """服务端返回了 4xx/5xx。status_code 便于上层区分 400 与 429。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# -------------------------------- 客户端 --------------------------------


class RawLLMClient:
    """一个最小可用的异步 LLM 客户端。

    用法：
        async with RawLLMClient() as client:
            result = await client.chat([{"role": "user", "content": "你好"}])
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    # -- 生命周期：连接池必须显式关闭 --

    async def __aenter__(self) -> RawLLMClient:
        # 四段超时分开设：连不上、读太慢、写太慢、等连接池，是四种不同故障
        timeout = httpx.Timeout(
            connect=5.0,
            read=self._settings.request_timeout_s,
            write=10.0,
            pool=5.0,
        )
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        self._client = httpx.AsyncClient(
            # base_url 指向 ".../v1"，请求用相对路径 "chat/completions"。
            # 实测 httpx 0.28 会把相对路径拼在 base 路径之后（base 结尾带不带 /
            # 结果都是 /v1/chat/completions），但别依赖这个容错：
            # 有些库用 urljoin 语义，"/chat" 会把 "/v1" 整个吃掉。
            base_url=f"{self._settings.llm_base_url.rstrip('/')}/",
            timeout=timeout,
            limits=limits,
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None

    # -- 内部工具 --

    def _require(self) -> httpx.AsyncClient:
        if self._client is None:
            raise LLMError("客户端未初始化，请用 `async with RawLLMClient() as c:` 使用")
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self._settings.llm_api_key
        if key is not None:
            headers["Authorization"] = f"Bearer {key.get_secret_value()}"
        return headers

    def _raise_for_error(self, response: httpx.Response) -> dict[str, object]:
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                error = body.get("error") if isinstance(body, dict) else None
                detail = (error or {}).get("message") or json.dumps(body, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                detail = response.text[:200]
            raise LLMError(
                f"HTTP {response.status_code}: {detail}",
                status_code=response.status_code,
            )
        return response.json()

    # -- 对外方法 --

    async def chat(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
    ) -> ChatResult:
        """非流式调用：一次拿完整回答。"""
        client = self._require()
        model = model or self._settings.llm_model
        payload = {"model": model, "messages": messages, "stream": False}

        response = await client.post("chat/completions", json=payload, headers=self._headers())
        data = self._raise_for_error(response)

        choices = data.get("choices") or []
        content = ""
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
        usage = Usage(**data.get("usage", {}))

        return ChatResult(
            text=str(content),
            usage=usage,
            cost_cny=estimate_cost(usage, model),
            model=model,
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        include_usage: bool = True,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用：逐块产出文本增量。

        include_usage=True 时会额外发 stream_options，服务端才会在最后
        一个块里带 usage。不传的话，流式响应里通常没有 token 数。
        """
        client = self._require()
        model = model or self._settings.llm_model
        payload: dict[str, object] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if include_usage:
            payload["stream_options"] = {"include_usage": True}

        async with client.stream(
            "POST", "chat/completions", json=payload, headers=self._headers()
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                self._raise_for_error(response)

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue  # 空行、注释行（: ping）、event: 行都跳过
                data = line[len("data:") :].strip()
                if not data or data == "[DONE]":
                    if data == "[DONE]":
                        break
                    continue

                event = json.loads(data)
                choices = event.get("choices") or []
                delta = ""
                if choices:
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                raw_usage = event.get("usage")

                if delta or raw_usage:
                    yield StreamChunk(
                        delta=delta,
                        usage=Usage(**raw_usage) if raw_usage else None,
                    )
```

### 4.3 `scripts/llm_demo.py`（6 个场景）

六个场景分别是：401 无 key、非流式、流式、流式不带 usage、400 顺序错、读超时。

**一个容易踩的坑**：必须用 `-m` 运行。

```powershell
uv run python -m scripts.llm_demo      # ✅
uv run python scripts/llm_demo.py      # ❌ ModuleNotFoundError: No module named 'scripts'
```

原因：直接跑文件时 `sys.path[0]` 变成 `scripts/` 目录本身，`import scripts.mock_llm_server` 自然找不到；`-m` 会把**项目根**放进 `sys.path`，包才可导入。完整实现见 [scripts/llm_demo.py](../../servicedesk-agent/scripts/llm_demo.py)。

### 4.4 `tests/test_raw_client.py`（全文）

```python
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
```

---

## 5. 逐块拆解

### 5.1 为什么 `messages` 是一个 dict 列表

真实 payload 里没有 `class Message`，只有 JSON。用 `TypedDict` 而不是 `pydantic.BaseModel` 描述它，是因为**它只用来发出去，不需要校验**——校验应该发生在服务端（它返回 400），你在这里重复校验只是浪费。

而 `Usage` 用 `pydantic`，是因为它要从**服务端返回的不完全可信的 JSON** 里解析出来，且有默认值兜底。

> 一句话：**出站用 TypedDict，入站用 BaseModel。**

### 5.2 四段超时为什么必须分开

```python
httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
```

| 字段 | 覆盖什么 | 不设会怎样 |
|---|---|---|
| `connect` | TCP/TLS 握手 | DNS 挂了，请求卡 30 秒 |
| `read` | 等下一块数据 | 服务端挂住不发数据，进程永久卡死 |
| `write` | 上传请求体 | 大请求在网络差时卡住 |
| `pool` | 等连接池空位 | 高并发下排队，看起来像"服务慢" |

最常见的错误是**只设一个数字**（`timeout=30`），它同时套在这四段上——于是"连不上"要等 30 秒才报错，用户早就关页面了。

### 5.3 SSE 为什么必须用 `aiter_lines()`

```python
async for line in response.aiter_lines():
```

不能自己按 `\n` 切 `response.text`，也不能按网络 chunk 切。原因：**TCP 是字节流，不保证边界**。一个 SSE 事件可能被拆成两个网络包，也可能两个事件挤在一个包里。

`aiter_lines()` 干的事就是"不管包怎么切，我保证按行给你"。这样每行一定是完整的 `data: {...}`。

实测在 §6.1：用 `resp.aread()` 先收完再解析，首字节延迟从 `0.006s` 涨到 `0.138s`——**功能没错，体验全毁**。

### 5.4 费用公式与"1M 单价"的换算

```python
(prompt_tokens * 输入单价 + completion_tokens * 输出单价) / 1_000_000
```

因为价目表按**百万 token** 报价，所以要除 `1e6`。实测：

```
prompt    : 14 tokens
completion: 9 tokens
本次费用  : ¥0.000015
估算 1000 次: ¥0.0150
```

**注意这是 mock 的 token 数**，真实调用会大得多（一次带 RAG 上下文的问答通常 1000–4000 输入 token）。

> ⚠️ `PRICING` 里的数字是**示例**，不是报价。上线前必须按你所用模型的官网价目改写；价格会变，代码里写死的价目表一定要留注释说明"最后核对日期"。

### 5.5 `include_usage`：一条被无数人忽略的静默失败

```python
payload["stream_options"] = {"include_usage": True}
```

不传这一行，流式响应的 `usage` 就是 `None`。实测（§7 场景 4）：

```
include_usage=False -> usage = None
```

**它不报错、不警告，你只是从此算不出这个请求花了多少钱。** 等月底看到账单，你会以为是"模型涨价了"。

### 5.6 为什么要 `async with`，而不是直接 `AsyncClient()`

`httpx.AsyncClient` 内部维护**连接池**。用了不关，Python 会在程序退出时打印：

```
Unclosed client session
```

在高并发服务里更糟：连接池泄漏，最后 `pool` 超时遍地开花。`async with` 保证 `__aexit__` 里一定 `aclose()`——哪怕是异常路径。

### 5.7 `_require()` 那三行是干嘛的

```python
def _require(self) -> httpx.AsyncClient:
    if self._client is None:
        raise LLMError("客户端未初始化，请用 `async with ...` 使用")
```

防的是"忘了 `async with`，直接 `await client.chat(...)`"。没有这三行，你会得到 `AttributeError: 'NoneType' object has no attribute 'post'`——一个完全指错方向的报错。测试里 `test_client_raises_when_used_outside_context_manager` 就是钉这个行为。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：先收完再解析 = 假流式

把 `stream()` 里的逐行迭代改成"先 `aread()` 全部收下，再解析"：

```python
async with client.stream("POST", "chat/completions", json=payload, headers=...) as response:
    await response.aread()          # ← 罪魁祸首：等全部内容到齐
    for line in response.text.splitlines():
        ...
```

实测首字节延迟：

```
缓冲式(aread) 首字节: 0.138s
逐行式(aiter_lines) 首字节: 0.006s
```

**功能一模一样，输出文字一模一样，但用户体验差 23 倍。** 这就是"看起来做了流式"和"真的做了流式"的区别。

放到生产里更明显：一个 800 token 的回答，真流式首字 0.3 秒出现，假流式要等 8 秒才吐第一个字——用户早就走了。

**修法**：`await response.aread()` 只能用在"要读错误体"的场景（比如 4xx 时先读 body 再抛异常），正常路径必须 `aiter_lines()`。

### 6.2 实验 B：忘了 `include_usage`，成本变成黑洞

把 `stream()` 的默认值改成 `include_usage: bool = False`（或者调用时不传），跑场景 4：

```
include_usage=False -> usage = None
```

后果链条：

1. 你拿不到 `completion_tokens` → 算不出 `cost_cny`
2. 算不出单次成本 → 算不出"每用户每天花多少钱"
3. 算不出 → 做不了预算告警、做不了成本优化、做不了定价

**它不报错，所以永远不会有人来修。** 只有当账单超预期时，你才发现"我们一直在盲飞"。

**修法**：

```diff
- async for chunk in client.stream(USER):
+ async for chunk in client.stream(USER, include_usage=True):
```

并且在 `StreamChunk.usage is None` 时**记一条告警日志**，别静默。

---

## 7. 验证与预期输出

本节实测环境：Windows 11 / Python 3.12.13 / httpx 0.28.1 / 全本地 mock。

```powershell
cd servicedesk-agent
uv run python -m scripts.llm_demo
```

实测输出：

```
mock 服务已启动: http://127.0.0.1:61268/v1

================================================================
场景 1：没配 key 会拿到什么
================================================================
  status_code: 401
  异常信息   : HTTP 401: 缺少 Authorization: Bearer <key>
  >>> 401 的原因是「请求头里没有 Bearer」，不是「网络不通」。
  >>> 这就是 1.1 里 has_llm_key 存在的意义：别让空 key 走到这一步。

================================================================
场景 2：非流式调用 —— 一次拿完整回答 + token + 费用
================================================================
  回答      : 异步不是让计算更快，而是让等待重叠。
  模型      : mock-model
  prompt    : 14 tokens
  completion: 9 tokens
  合计      : 23 tokens
  本次费用  : ¥0.000015
  估算 1000 次: ¥0.0150
  耗时      : 0.010s
  >>> 费用是「按 token 计」，不是「按次计」——所以上下文越长越贵。

================================================================
场景 3：流式调用 —— 边生成边吐字
================================================================
  输出: 异步不是让计算更快，而是让等待重叠。
  首字延迟  : 0.007s   ← 用户感知到的「快」
  总耗时    : 0.134s
  usage     : prompt_tokens=14 completion_tokens=9 total_tokens=23
  >>> 首字延迟才是体感关键：总耗时没变，但用户 0.02s 就看到东西了。

================================================================
场景 4：流式不显式要 usage，就拿不到 token 数
================================================================
  include_usage=False -> usage = None
  >>> 原因：流式响应默认不带 usage，必须发 stream_options.include_usage。
  >>> 这也是「流式调用算不清成本」的常见根因。

================================================================
场景 5：messages 顺序错了，服务端会拒绝
================================================================
  status_code: 400
  异常信息   : HTTP 400: messages[1] 是 tool 消息，但前一条不是带 tool_calls 的 assistant 消息 —— 顺序错了
  >>> tool 消息必须紧跟在「带 tool_calls 的 assistant」之后。这是 2.1 的伏笔。

================================================================
场景 6：读超时 —— 别让一个慢请求挂死整个进程
================================================================
  捕获 ReadTimeout，用时 0.308s
  >>> read=0.3s 是我们自己设的上限；真实服务再慢也拖不住我们。
```

跑测试：

```powershell
uv run pytest
```

```
...............                                                          [100%]
15 passed in 2.06s
```

（1.1 的 3 个 + 1.2 的 3 个 + 本节的 9 个。）

---

## 8. 生产环境踩坑实录

### 坑 1：401 和 404 傻傻分不清

**现象**

线上报 `LLMError: HTTP 404`，但本地同一个 Key 好好的。

**定位方法**

```python
logger.error("请求失败", extra={"url": str(response.request.url)})
```

或者直接在客户端里打一行：

```python
print(response.request.url)   # 一眼看出发到哪去了
```

**根因**

`base_url` 配错了。典型三种：

- 配成 `https://api.openai.com`（少了 `/v1`）→ 打到 `/chat/completions` → 404
- 配成 `https://api.openai.com/v1/chat/completions`（把路径也写进去了）→ 拼成 `/v1/chat/completions/chat/completions` → 404
- 用了第三方兼容网关，但漏了它的路径前缀

**修法**

```diff
- LLM_BASE_URL=https://api.openai.com
+ LLM_BASE_URL=https://api.openai.com/v1
```

**顺手加固**：启动自检（1.1 的 `python -m servicedesk`）里把 `base_url` 打出来；客户端在 404 时把**完整 URL** 写进异常消息，而不是只说"HTTP 404"。

### 坑 2：SSE 解析偶发 `JSONDecodeError`

**现象**

平时好好的，偶尔报：

```
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
```

而且是**偶发**——压测时更容易出现。

**定位方法**

把出问题的那一行原样打印出来（`repr(line)`），一定会看到这类东西：

```
'data: {"choices":[{"delta":{"content":"异'      ← 半行
```

或者：

```
': ping'                                         ← 注释行，不是 JSON
```

**根因**

自己按网络 chunk 或 `\n` 切分，切在了半行上；或者没有过滤空行、`:` 开头的注释行、`event:` 行。

**修法**

```diff
- async for chunk in response.aiter_bytes():
-     for line in chunk.decode().split("\n"):
+ async for line in response.aiter_lines():
+     if not line.startswith("data:"):
+         continue
+     data = line[len("data:"):].strip()
+     if not data or data == "[DONE]":
+         continue
```

**顺手加固**：解析失败时**不要直接抛**，先 `logger.warning("跳过无法解析的 SSE 行", extra={"line": line[:200]})` 再 `continue`。一行脏数据不该让整个对话崩掉。

### 坑 3：`Unclosed client session` / 连接池耗尽

**现象**

程序退出时刷屏：

```
Unclosed client session
client_session: <aiohttp.ClientSession object at 0x...>
```

或者运行几小时后开始报 `PoolTimeout`、`Too many open files`。

**定位方法**

```python
import asyncio
print(len(asyncio.all_tasks()))   # 任务数持续上涨 = 泄漏
```

再查代码里所有 `AsyncClient(` / `ClientSession(`，确认每一个都有对应的 `aclose()`。

**根因**

在函数里 `client = httpx.AsyncClient()` 却从不关闭。每次调用新建一个客户端、新建一套连接，旧的全部泄漏。

**修法**

```diff
- client = httpx.AsyncClient()
- response = await client.post(...)
+ async with httpx.AsyncClient() as client:
+     response = await client.post(...)
```

更好的做法：**整个应用只用一个长生命周期的 Client**（本节的 `RawLLMClient` 就是干这个），在应用启动时创建、关闭时释放。连接池复用的收益是"省掉每次的 TCP + TLS 握手"，在 500ms 的 LLM 调用里不明显，但在 20ms 的工具调用里非常明显。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么流式响应里经常拿不到 usage？</b></summary>

因为流式响应默认不返回 token 统计。必须在请求里加 `"stream_options": {"include_usage": true}`，服务端才会在最后一个 chunk 里附带 `usage`。不传不报错，只是静默地拿不到——所以"算不清成本"往往不是计费系统的问题，而是这里漏了。

</details>

<details>
<summary><b>Q2：为什么 `tool` 消息必须紧跟在带 `tool_calls` 的 assistant 之后？</b></summary>

因为对话是**有序的因果链**：模型先表达"我要调用某个工具"（assistant + tool_calls），你的代码执行后把结果回填（tool + tool_call_id），模型才知道这个结果对应哪个请求。跳步就等于"你没问就自答"，模型无法把结果关联回它的调用意图，服务端会直接 400。

</details>

<details>
<summary><b>Q3：`httpx.Timeout` 为什么要分 connect / read / write / pool 四段？</b></summary>

因为它们对应四种完全不同的故障：连不上（DNS/TLS）、读不到（服务端挂住）、写不出（上传卡住）、等不到连接（池满排队）。只设一个数字会让"连不上"也等满 30 秒才报错，用户感知极差。分开设才能让每种故障在各自合理的时限内暴露。

</details>

### 9.2 动手任务：给客户端加"成本闸"

**要求**

实现一个包装函数：

```python
async def chat_with_budget(
    client: RawLLMClient,
    messages: list[Message],
    *,
    budget_cny: float,
) -> ChatResult:
    ...
```

1. 调用前先**估算**输入 token（用 `len(json.dumps(messages)) // 3` 这类粗暴估算即可）。
2. 如果估算出的**最低可能成本**（按输入 token × 输入单价）已经超过 `budget_cny`，**不发起请求**，直接抛 `LLMError`。
3. 调用完成后，如果实际 `cost_cny` 超过预算，记一条 warning（不抛异常，因为钱已经花了）。

**验收标准**

- `budget_cny=0.000001` 时，抛 `LLMError` 且**没有发出 HTTP 请求**（可以用 mock 的请求计数验证：在 mock 里加一个 `request_count`，断言它没变）。
- `budget_cny=1.0` 时正常返回，`cost_cny` 有值。
- 补 2 个 pytest 用例覆盖这两个分支。

**提示**：这其实就是生产里的"成本闸"（cost guard），5.7 会正经实现一版。现在先体会"在花钱之前拦住"。

### 9.3 拓展挑战

给 `stream()` 加一个 `on_first_token` 回调，在收到第一个 delta 时触发，用来统计"首字延迟"并上报。

**思考**：首字延迟（TTFT）和总耗时（e2e）是两个不同的体验指标，你觉得哪个更该进 SLO？为什么流式场景下"总耗时"的意义会被削弱？

---

## 10. 本节小结与延伸

**三点回顾**

1. **调 LLM 就是发一次 `POST /chat/completions`**，请求体是一个 dict，响应体也是一个 dict。看清这一层，你才能在出错时知道该看请求还是响应。
2. **流式不只是"边打边显示"，而是首字延迟从 0.138s 降到 0.006s 的体验差距**；而 `include_usage` 决定了你能不能算清成本——两者都不报错，全靠你自己记得。
3. **四段超时 + 连接池复用 + 异常带 status_code**，这三件事把"能跑的脚本"抬到"不怕线上出事的组件"。

**已知未验证项（诚实记录）**

- 真实 API 路径未跑过（本机无 Key）。切换方式：`.env` 填 `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`，客户端代码零改动。
- `PRICING` 表里的价格是**示例值**，不是任何一家厂商的承诺报价；上线前必须按官网核对并写上核对日期。

**延伸阅读**

- OpenAI Chat Completions API 参考（请求/响应字段逐项说明）：https://platform.openai.com/docs/api-reference/chat
- SSE（Server-Sent Events）规范：https://html.spec.whatwg.org/multipage/server-sent-events.html
- httpx 的 Timeout 与 Limits：https://www.python-httpx.org/advanced/timeouts/
- httpx 流式响应：https://www.python-httpx.org/async/#streaming-responses

---

## 修订记录

- 2026-11-17 初稿：全链路对本地 mock 实测，§7 为真实输出；§6.1 首字节延迟 0.138s vs 0.006s、§6.2 usage=None 均实测
- 2026-11-17 修正：原注释断言"base_url 不带结尾斜杠会把 /v1 吃掉"，实测 httpx 0.28 三种写法均拼成 `/v1/chat/completions`，已改为如实描述并保留"别依赖该容错"的告警
- 2026-11-17 补充：mock 服务端捕获 `ConnectionError`（含 WinError 10053/10054），消除客户端提前断开时的 stderr 噪音
