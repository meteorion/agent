# file: scripts/m2_agent.py
"""2.7 的场景：M2 验收——Agent 一次回答里串起三个 MCP 工具。

    query_asset → query_permission → search_knowledge

用法：
    uv run python -m scripts.m2_agent
"""

from __future__ import annotations

import asyncio

from langchain_mcp_adapters.client import MultiServerMCPClient

from scripts.mock_llm_server import start_mock_server
from servicedesk.config import Settings
from servicedesk.graph.tools_bridge import McpToolBridge, ToolCatalog, split_namespaced
from servicedesk.llm.client import LLMClient
from servicedesk.logging_setup import configure_logging
from servicedesk.mcp.http_app import create_server
from servicedesk.tools.loop import run_tool_loop, summarize

QUESTION = "NB-1024 是谁在用？他有 VPN 权限吗？如果认证失败该怎么办？"
WHITELIST = ["query_asset", "query_permission", "search_knowledge"]


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


async def main() -> int:
    configure_logging(level="ERROR")

    mcp_server, mcp_url = create_server()
    llm_server, llm_url = start_mock_server()
    print(f"MCP Server : {mcp_url}")
    print(f"Mock LLM   : {llm_url}")

    try:
        header("第 1 步：用真实适配器把 MCP 工具取下来")
        client = MultiServerMCPClient(
            {"demo": {"url": mcp_url, "transport": "streamable_http"}}
        )
        raw_tools = await client.get_tools()
        print(f"  拿到 {len(raw_tools)} 个工具：{[t.name for t in raw_tools]}")
        print(f"  类型：{type(raw_tools[0]).__name__}（LangChain BaseTool）")

        header("第 2 步：命名空间 + token 预算")
        full_bridge = McpToolBridge(raw_tools, server="demo")
        print(f"  命名空间后：{full_bridge.names()}")
        print(f"  全量 schema 约 {full_bridge.schema_tokens()} tokens")

        catalog = ToolCatalog(bridge=full_bridge)
        print("\n  目录（discover 阶段只拿元信息，不产生调用）：")
        for info in catalog.discover():
            print(f"    - {info.name:<34} {info.description[:32]}…")

        trimmed = McpToolBridge(raw_tools, server="demo", include=WHITELIST)
        print(f"\n  白名单 {WHITELIST}")
        print(f"  精简后 schema 约 {trimmed.schema_tokens()} tokens"
              f"（省 {full_bridge.schema_tokens() - trimmed.schema_tokens()}）")

        header("第 3 步：把 MCP 工具接回 2.1 的工具循环")
        registry = trimmed.to_registry()
        print(f"  registry 里的工具：{registry.names()}")
        for name in registry.names():
            server, original = split_namespaced(name)
            print(f"    {name:<34} → server={server} tool={original}")

        header(f"第 4 步：端到端问答\n  问题：{QUESTION}")
        settings = Settings(
            _env_file=None,
            llm_base_url=llm_url,
            llm_model="mock-model",
            llm_api_key="mock-key",
        )
        async with LLMClient(settings) as llm:
            result = await run_tool_loop(llm, registry, QUESTION, max_rounds=3)

        print(f"\n  轮次：{result.rounds}")
        print(f"  调用工具：{result.called_tools}")
        for record in result.calls:
            server, original = split_namespaced(record.name)
            status = "ok" if record.result.ok else f"失败({record.result.error})"
            print(f"    - {original:<18} [{server}] {status}  {record.result.duration_ms:.0f}ms")
        print(f"\n  回答：{result.answer[:300]}")
        print(f"\n  摘要：{summarize(result)}")

        header("第 5 步：M2 验收对照")
        called = {split_namespaced(name)[1] for name in result.called_tools}
        checks = [
            ("串起 ≥3 个 MCP 工具", len(called) >= 3, sorted(called)),
            ("含查资产", "query_asset" in called, "query_asset"),
            ("含查权限", "query_permission" in called, "query_permission"),
            ("含知识库检索", "search_knowledge" in called, "search_knowledge"),
            ("只用了白名单内的工具", called <= set(WHITELIST), sorted(called - set(WHITELIST))),
        ]
        for label, ok, detail in checks:
            print(f"  {'✅' if ok else '❌'} {label:<22} {detail}")

        passed = all(ok for _, ok, _ in checks)
        print(f"\n  M2 验收：{'通过' if passed else '未通过'}")
        return 0 if passed else 1
    finally:
        mcp_server.shutdown()
        llm_server.shutdown()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
