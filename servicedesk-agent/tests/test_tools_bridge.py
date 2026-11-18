# file: tests/test_tools_bridge.py
"""2.7 的断言：命名空间、白名单、token 预算、端到端串三个工具。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from scripts.mock_llm_server import start_mock_server
from servicedesk.config import Settings
from servicedesk.graph.tools_bridge import (
    McpToolBridge,
    ToolCatalog,
    estimate_schema_tokens,
    namespaced,
    split_namespaced,
)
from servicedesk.llm.client import LLMClient
from servicedesk.mcp.http_app import create_server
from servicedesk.tools.loop import run_tool_loop


# ------------------------------ 测试替身 ------------------------------


class EchoParams(BaseModel):
    text: str = "hi"


class FakeTool:
    """模拟 langchain_mcp_adapters 返回的 BaseTool（args_schema 是 dict）。"""

    def __init__(self, name: str, description: str = "假工具") -> None:
        self.name = name
        self.description = description
        self.args_schema = EchoParams.model_json_schema()

    def get_input_schema(self) -> type[BaseModel]:
        return EchoParams

    async def ainvoke(self, kwargs: dict) -> list[dict]:
        return [{"type": "text", "text": f'{{"echo": "{kwargs.get("text", "")}"}}'}]


# ------------------------------ 命名空间 ------------------------------


def test_namespacing_roundtrip() -> None:
    full = namespaced("demo", "query_asset")
    assert full == "mcp__demo__query_asset"
    assert split_namespaced(full) == ("demo", "query_asset")


def test_split_without_namespace() -> None:
    assert split_namespaced("plain_tool") == (None, "plain_tool")


def test_namespaced_tools_from_two_servers_do_not_collide() -> None:
    assert namespaced("a", "search") != namespaced("b", "search")


# ------------------------------ 桥接器 ------------------------------


def test_whitelist_filters_tools() -> None:
    tools = [FakeTool("alpha"), FakeTool("beta"), FakeTool("gamma")]
    bridge = McpToolBridge(tools, server="demo", include=["alpha", "gamma"])

    assert bridge.names() == ["mcp__demo__alpha", "mcp__demo__gamma"]
    assert [info.original for info in bridge.infos()] == ["alpha", "gamma"]


def test_whitelist_with_unknown_name_raises() -> None:
    with pytest.raises(ValueError):
        McpToolBridge([FakeTool("alpha")], server="demo", include=["missing"])


def test_schema_tokens_grow_with_tool_count() -> None:
    few = McpToolBridge([FakeTool("a")], server="demo")
    many = McpToolBridge([FakeTool("a"), FakeTool("b"), FakeTool("c")], server="demo")
    assert many.schema_tokens() > few.schema_tokens()
    assert estimate_schema_tokens([]) >= 1


def test_to_registry_proxies_calls() -> None:
    bridge = McpToolBridge([FakeTool("echo")], server="demo")
    registry = bridge.to_registry()

    assert registry.names() == ["mcp__demo__echo"]
    import asyncio

    outcome = asyncio.run(registry.call("mcp__demo__echo", '{"text": "你好"}'))
    assert outcome.ok is True
    assert "你好" in outcome.content


def test_tool_catalog_loads_subset() -> None:
    tools = [FakeTool("alpha"), FakeTool("beta")]
    catalog = ToolCatalog(bridge=McpToolBridge(tools, server="demo"))

    assert [info.original for info in catalog.discover()] == ["alpha", "beta"]
    assert catalog.load(["beta"]).names() == ["mcp__demo__beta"]

    with pytest.raises(ValueError):
        catalog.load(["nope"])


# ------------------------------ M2 验收 ------------------------------


@pytest.fixture(scope="module")
def two_servers():
    mcp_server, mcp_url = create_server()
    llm_server, llm_url = start_mock_server()
    yield mcp_url, llm_url
    mcp_server.shutdown()
    llm_server.shutdown()


async def test_end_to_end_three_mcp_tools_in_one_answer(two_servers) -> None:
    """M2 验收：真实适配器 + 真实 MCP Server，一次回答里串起三个工具。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    mcp_url, llm_url = two_servers
    client = MultiServerMCPClient({"demo": {"url": mcp_url, "transport": "streamable_http"}})
    raw_tools = await client.get_tools()
    assert len(raw_tools) == 5

    bridge = McpToolBridge(
        raw_tools,
        server="demo",
        include=["query_asset", "query_permission", "search_knowledge"],
    )
    registry = bridge.to_registry()

    settings = Settings(
        _env_file=None, llm_base_url=llm_url, llm_model="mock-model", llm_api_key="mock-key"
    )
    async with LLMClient(settings) as llm:
        result = await run_tool_loop(
            llm,
            registry,
            "NB-1024 是谁在用？他有 VPN 权限吗？如果认证失败该怎么办？",
            max_rounds=3,
        )

    called = {split_namespaced(name)[1] for name in result.called_tools}
    assert called == {"query_asset", "query_permission", "search_knowledge"}
    assert result.rounds == 2, "三个工具应在同一轮并行发出"
    assert all(record.result.ok for record in result.calls)
    assert "张伟" in result.answer
