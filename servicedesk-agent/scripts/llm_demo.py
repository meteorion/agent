# file: scripts/llm_demo.py
"""1.3 的六个场景：把 LLM 调用扒成一次普通 HTTP 请求。

全程对本地 mock 服务发请求，不需要 API Key、不产生任何费用。

用法（注意是 -m，不是直接跑文件）：
    uv run python -m scripts.llm_demo

为什么必须 -m：直接 `python scripts/llm_demo.py` 时，sys.path[0] 会变成
scripts/ 目录本身，于是 `import scripts.mock_llm_server` 找不到包。
`-m` 会把当前目录（项目根）放进 sys.path，包才可导入。
"""

from __future__ import annotations

import asyncio
import time

import httpx

from scripts.mock_llm_server import start_mock_server
from servicedesk.config import Settings
from servicedesk.llm.raw_client import LLMError, RawLLMClient


def header(title: str) -> None:
    print()
    print("=" * 64)
    print(title)
    print("=" * 64)


def make_settings(
    base_url: str,
    *,
    api_key: str | None = "mock-key",
    timeout_s: float = 5.0,
) -> Settings:
    """构造一个只指向本地 mock 的 Settings，完全不读 .env。"""
    return Settings(
        _env_file=None,
        llm_base_url=base_url,
        llm_model="mock-model",
        llm_api_key=api_key,
        request_timeout_s=timeout_s,
    )


USER = [{"role": "user", "content": "异步到底解决什么问题？"}]


# ------------------------------ 场景 1：401 ------------------------------


async def demo_missing_key(base_url: str) -> None:
    header("场景 1：没配 key 会拿到什么")
    async with RawLLMClient(make_settings(base_url, api_key=None)) as client:
        try:
            await client.chat(USER)
        except LLMError as exc:
            print(f"  status_code: {exc.status_code}")
            print(f"  异常信息   : {exc}")
    print("  >>> 401 的原因是「请求头里没有 Bearer」，不是「网络不通」。")
    print("  >>> 这就是 1.1 里 has_llm_key 存在的意义：别让空 key 走到这一步。")


# --------------------------- 场景 2：非流式 ---------------------------


async def demo_chat(base_url: str) -> None:
    header("场景 2：非流式调用 —— 一次拿完整回答 + token + 费用")
    async with RawLLMClient(make_settings(base_url)) as client:
        started = time.perf_counter()
        result = await client.chat(USER)
        elapsed = time.perf_counter() - started

        print(f"  回答      : {result.text}")
        print(f"  模型      : {result.model}")
        print(f"  prompt    : {result.usage.prompt_tokens} tokens")
        print(f"  completion: {result.usage.completion_tokens} tokens")
        print(f"  合计      : {result.usage.total_tokens} tokens")
        print(f"  本次费用  : ¥{result.cost_cny:.6f}")
        print(f"  估算 1000 次: ¥{result.cost_cny * 1000:.4f}")
        print(f"  耗时      : {elapsed:.3f}s")
    print("  >>> 费用是「按 token 计」，不是「按次计」——所以上下文越长越贵。")


# ---------------------------- 场景 3：流式 ----------------------------


async def demo_stream(base_url: str) -> None:
    header("场景 3：流式调用 —— 边生成边吐字")
    async with RawLLMClient(make_settings(base_url)) as client:
        started = time.perf_counter()
        first_token_at = 0.0
        usage = None
        collected: list[str] = []

        print("  输出: ", end="")
        async for chunk in client.stream(USER):
            if chunk.delta:
                if first_token_at == 0.0:
                    first_token_at = time.perf_counter() - started
                collected.append(chunk.delta)
                print(chunk.delta, end="", flush=True)
            if chunk.usage is not None:
                usage = chunk.usage
        print()

        total = time.perf_counter() - started
        print(f"  首字延迟  : {first_token_at:.3f}s   ← 用户感知到的「快」")
        print(f"  总耗时    : {total:.3f}s")
        print(f"  usage     : {usage}")
    print("  >>> 首字延迟才是体感关键：总耗时没变，但用户 0.02s 就看到东西了。")


# -------------------- 场景 4：流式但没要 usage --------------------


async def demo_stream_without_usage(base_url: str) -> None:
    header("场景 4：流式不显式要 usage，就拿不到 token 数")
    async with RawLLMClient(make_settings(base_url)) as client:
        usage = None
        async for chunk in client.stream(USER, include_usage=False):
            if chunk.usage is not None:
                usage = chunk.usage
        print(f"  include_usage=False -> usage = {usage}")
    print("  >>> 原因：流式响应默认不带 usage，必须发 stream_options.include_usage。")
    print("  >>> 这也是「流式调用算不清成本」的常见根因。")


# --------------------------- 场景 5：400 ---------------------------


async def demo_bad_order(base_url: str) -> None:
    header("场景 5：messages 顺序错了，服务端会拒绝")
    bad_messages = [
        {"role": "user", "content": "帮我查一下工单 T-1001"},
        {"role": "tool", "content": "工单状态：处理中", "tool_call_id": "call_1"},
    ]
    async with RawLLMClient(make_settings(base_url)) as client:
        try:
            await client.chat(bad_messages)
        except LLMError as exc:
            print(f"  status_code: {exc.status_code}")
            print(f"  异常信息   : {exc}")
    print("  >>> tool 消息必须紧跟在「带 tool_calls 的 assistant」之后。这是 2.1 的伏笔。")


# ---------------------------- 场景 6：超时 ----------------------------


async def demo_timeout(base_url: str) -> None:
    header("场景 6：读超时 —— 别让一个慢请求挂死整个进程")
    slow = [{"role": "user", "content": "#slow 这个请求很慢"}]
    async with RawLLMClient(make_settings(base_url, timeout_s=0.3)) as client:
        started = time.perf_counter()
        try:
            await client.chat(slow)
        except httpx.ReadTimeout:
            print(f"  捕获 ReadTimeout，用时 {time.perf_counter() - started:.3f}s")
    print("  >>> read=0.3s 是我们自己设的上限；真实服务再慢也拖不住我们。")


async def main() -> None:
    # Windows 的 ProactorEventLoop 有个老毛病：对端强关连接时，
    # 会在"连接丢失"的回调里抛 ConnectionResetError，把 stderr 刷满。
    # 我们主动忽略这一种，其余异常照常打印。
    loop = asyncio.get_running_loop()

    def _quiet_reset(loop_: asyncio.AbstractEventLoop, context: dict) -> None:
        if isinstance(context.get("exception"), ConnectionResetError):
            return
        loop_.default_exception_handler(context)

    loop.set_exception_handler(_quiet_reset)

    server, base_url = start_mock_server()
    print(f"mock 服务已启动: {base_url}")
    try:
        await demo_missing_key(base_url)
        await demo_chat(base_url)
        await demo_stream(base_url)
        await demo_stream_without_usage(base_url)
        await demo_bad_order(base_url)
        await demo_timeout(base_url)
    finally:
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
