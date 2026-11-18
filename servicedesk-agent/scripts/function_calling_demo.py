# file: scripts/function_calling_demo.py
"""2.1 的场景：schema 长什么样、模型怎么选工具、工具失败会怎样。

用法：
    uv run python -m scripts.function_calling_demo
"""

from __future__ import annotations

import asyncio
import json

from scripts.mock_llm_server import start_mock_server
from servicedesk.config import Settings
from servicedesk.llm.client import LLMClient
from servicedesk.logging_setup import configure_logging
from servicedesk.tools.builtin import build_default_registry
from servicedesk.tools.loop import run_tool_loop, summarize
from servicedesk.tools.registry import inline_refs

QUESTIONS = [
    ("单工具", "NB-1024 这台设备的资产信息帮我查一下"),
    ("并列两个工具", "NB-1024 这台电脑是谁在用，他有 VPN 权限吗？"),
    ("不需要工具", "你好，请介绍一下你能做什么"),
]


def header(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def show_schema() -> None:
    registry = build_default_registry()
    schemas = registry.schemas()
    print(f"注册了 {len(schemas)} 个工具：{registry.names()}")
    print()

    first = schemas[0]
    raw = first["function"]["parameters"]
    print(f"工具 {first['function']['name']} 的参数 schema（已展开 $ref）：")
    print("  " + json.dumps(raw, ensure_ascii=False)[:400] + "…")
    print()
    print(f"  包含 '$defs' 吗：{'$defs' in raw}      （strict 模式要求没有）")
    print(f"  包含 '$ref' 吗：{'$ref' in json.dumps(raw)}")
    print()
    print("完整工具清单（模型看到的就是这些文字）：")
    for schema in schemas:
        function = schema["function"]
        print(f"  - {function['name']}")
        print(f"    {function['description'][:60]}…")
        props = function["parameters"].get("properties", {})
        for name, spec in props.items():
            print(f"      参数 {name}: {spec.get('description', '')[:50]}")


def show_raw_inline() -> None:
    """用一个带枚举的模型演示 $defs → 内联 的过程。"""
    from pydantic import BaseModel, Field

    from servicedesk.models.ticket import Priority

    class DemoParams(BaseModel):
        priority: Priority = Field(description="优先级")

    before = DemoParams.model_json_schema()
    after = inline_refs(before)
    print(f"  原始 schema 含 $defs：{'$defs' in before}")
    print(f"  展开后含 $defs：{'$defs' in after}")
    print(f"  展开前 priority 的写法：{before['properties']['priority']}")
    print(f"  展开后 priority 的写法：{after['properties']['priority']}")


async def main() -> int:
    configure_logging(level="ERROR")

    header("第 1 步：schema 从 pydantic 模型自动生成")
    show_schema()
    print()
    print("inline_refs 的作用：")
    show_raw_inline()

    server, base_url = start_mock_server()
    settings = Settings(
        _env_file=None,
        llm_base_url=base_url,
        llm_model="mock-model",
        llm_api_key="mock-key",
    )
    registry = build_default_registry()

    try:
        async with LLMClient(settings) as client:
            for label, question in QUESTIONS:
                header(f"第 2 步：{label} ——「{question}」")
                result = await run_tool_loop(
                    client,
                    registry,
                    question,
                    system="你是企业 IT 服务台助手，需要时调用工具获取真实数据。",
                )
                print(f"  轮次: {result.rounds}")
                print(f"  调用工具: {result.called_tools or '（无）'}")
                for record in result.calls:
                    status = "ok" if record.result.ok else f"失败({record.result.error})"
                    print(
                        f"    - {record.name}({record.arguments}) → {status}"
                        f"  {record.result.duration_ms:.0f}ms"
                    )
                print(f"  回答: {result.answer[:160]}")
                print(f"  摘要: {summarize(result)}")
    finally:
        server.shutdown()

    header("第 3 步：工具失败时，模型看到的是什么（全部是字符串）")
    cases = [
        ("未知工具", "query_wifi", "{}"),
        ("参数不是 JSON", "query_asset", "{hostname: NB-1024"),
        ("参数校验失败", "query_asset", '{"hostname": "x"}'),
        ("查不到数据（不是错误）", "query_asset", '{"hostname": "NB-9999"}'),
    ]
    for label, name, arguments in cases:
        outcome = await registry.call(name, arguments)
        print(f"\n  【{label}】{name}({arguments})")
        print(f"    ok={outcome.ok}  error={outcome.error}")
        print(f"    回喂给模型的内容：{outcome.content}")

    header("小结")
    print("  1. schema 由 pydantic 生成，改模型即改 schema；$defs 必须展开成内联。")
    print("  2. 模型返回的 arguments 是**字符串**，且可能是坏 JSON —— 一定要校验。")
    print("  3. 工具失败以字符串回喂，模型才有机会自我纠正；抛异常只会让对话中断。")
    print("  4. 「查不到」是正常结果，要和「工具出错」分开表达。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
