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
from dataclasses import dataclass, field
from typing import Any, NotRequired, TypedDict

import httpx
from pydantic import BaseModel

from servicedesk.config import Settings, get_settings


# ------------------------------- 数据契约 -------------------------------


class Message(TypedDict):
    """一条对话消息。role 的取值含义见 README 或 §5.1。

    2.1 起新增两个可选字段：
        tool_calls   只出现在 assistant 消息上，表示「模型要调这些工具」
        tool_call_id 只出现在 tool 消息上，用来对上某一次调用
    """

    role: str          # system | user | assistant | tool
    content: str
    tool_call_id: NotRequired[str]
    tool_calls: NotRequired[list[dict[str, Any]]]


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
class ToolCall:
    """模型发起的一次工具调用。

    arguments 是**字符串**，不是 dict —— 服务端就是这么返回的，
    而且它可能是非法 JSON（模型偶尔会写坏）。
    """

    id: str
    name: str
    arguments: str


@dataclass
class ChatResult:
    text: str
    usage: Usage
    cost_cny: float
    model: str
    tool_calls: list[ToolCall] = field(default_factory=list)


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
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> ChatResult:
        """非流式调用：一次拿完整回答。传了 tools 就允许模型发起工具调用。"""
        client = self._require()
        model = model or self._settings.llm_model
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"

        response = await client.post("chat/completions", json=payload, headers=self._headers())
        data = self._raise_for_error(response)

        choices = data.get("choices") or []
        content = ""
        tool_calls: list[ToolCall] = []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            for index, raw_call in enumerate(message.get("tool_calls") or []):
                function = raw_call.get("function") or {}
                tool_calls.append(
                    ToolCall(
                        id=str(raw_call.get("id") or f"call_{index}"),
                        name=str(function.get("name") or ""),
                        arguments=str(function.get("arguments") or ""),
                    )
                )
        usage = Usage(**data.get("usage", {}))

        return ChatResult(
            text=str(content),
            usage=usage,
            cost_cny=estimate_cost(usage, model),
            model=model,
            tool_calls=tool_calls,
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
