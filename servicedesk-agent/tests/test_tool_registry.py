# file: tests/test_tool_registry.py
"""2.1 的断言：schema 合法、参数校验、失败以字符串返回、工具循环顺序正确。"""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel, Field

from scripts.mock_llm_server import start_mock_server
from servicedesk.config import Settings
from servicedesk.errors import FatalError
from servicedesk.llm.client import LLMClient
from servicedesk.models.ticket import Priority
from servicedesk.tools.builtin import build_default_registry
from servicedesk.tools.loop import run_tool_loop, summarize
from servicedesk.tools.registry import ToolRegistry, inline_refs


class DemoParams(BaseModel):
    priority: Priority = Field(description="优先级")


# -------------------------------- schema --------------------------------


def test_inline_refs_resolves_enum() -> None:
    before = DemoParams.model_json_schema()
    assert "$defs" in before
    assert before["properties"]["priority"].get("$ref")

    after = inline_refs(before)
    assert "$defs" not in after
    assert "$ref" not in json.dumps(after)
    assert after["properties"]["priority"]["enum"] == ["P0", "P1", "P2", "P3"]


def test_registry_schemas_are_openai_shaped() -> None:
    registry = build_default_registry()
    schemas = registry.schemas()

    # 2.6 新增了 create_ticket（原本 3 个）
    assert len(schemas) == 5
    for schema in schemas:
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"] and function["description"]
        assert function["parameters"]["type"] == "object"
        assert "$defs" not in json.dumps(function["parameters"])


def test_schema_order_is_stable() -> None:
    """顺序稳定才有利于 prompt cache（2.5 会展开讲）。"""
    first = [schema["function"]["name"] for schema in build_default_registry().schemas()]
    second = [schema["function"]["name"] for schema in build_default_registry().schemas()]
    assert first == second == sorted(first)


def test_duplicate_tool_name_rejected() -> None:
    registry = ToolRegistry()
    registry.register(name="dup", description="d", params_model=DemoParams)(lambda: None)
    with pytest.raises(ValueError):
        registry.register(name="dup", description="d", params_model=DemoParams)(lambda: None)


# -------------------------------- 调用 --------------------------------


async def test_unknown_tool_returns_error_string() -> None:
    registry = build_default_registry()
    outcome = await registry.call("query_wifi", "{}")
    assert outcome.ok is False
    assert outcome.error == "unknown_tool"
    assert "可用工具" in outcome.content


async def test_bad_json_returns_error_string() -> None:
    registry = build_default_registry()
    outcome = await registry.call("query_asset", "{hostname: NB-1024")
    assert outcome.ok is False
    assert outcome.error == "bad_json"
    assert "JSON" in outcome.content


async def test_invalid_params_lists_field() -> None:
    registry = build_default_registry()
    outcome = await registry.call("query_asset", '{"hostname": "x"}')
    assert outcome.ok is False
    assert outcome.error == "invalid_params"
    assert "hostname" in outcome.content


async def test_success_returns_json_content() -> None:
    registry = build_default_registry()
    outcome = await registry.call("query_asset", '{"hostname": "NB-1024"}')
    assert outcome.ok is True
    payload = json.loads(outcome.content)
    assert payload["owner"].startswith("张伟")
    assert outcome.duration_ms >= 0


async def test_not_found_is_success_not_error() -> None:
    """「查不到」是正常业务结果，不能被当成工具失败。"""
    registry = build_default_registry()
    outcome = await registry.call("query_asset", '{"hostname": "NB-9999"}')
    assert outcome.ok is True
    assert outcome.error is None
    assert json.loads(outcome.content)["found"] is False


async def test_timeout_returns_error_string() -> None:
    registry = ToolRegistry()

    class SlowParams(BaseModel):
        seconds: float = 1.0

    async def slow(seconds: float) -> dict:
        await asyncio.sleep(seconds)
        return {"done": True}

    registry.register(
        name="slow_tool", description="慢工具", params_model=SlowParams, timeout_s=0.05
    )(slow)

    outcome = await registry.call("slow_tool", '{"seconds": 1.0}')
    assert outcome.ok is False
    assert outcome.error == "timeout"


async def test_exception_returns_error_string() -> None:
    registry = ToolRegistry()

    class EmptyParams(BaseModel):
        pass

    async def boom() -> dict:
        raise RuntimeError("下游 500")

    registry.register(name="boom", description="会炸", params_model=EmptyParams)(boom)
    outcome = await registry.call("boom", "{}")
    assert outcome.ok is False
    assert outcome.error == "exception"
    assert "下游 500" in outcome.content


# ------------------------------ 工具循环 ------------------------------


@pytest.fixture(scope="module")
def mock_base_url():
    server, url = start_mock_server()
    yield url
    server.shutdown()


def make_settings(base_url: str) -> Settings:
    return Settings(
        _env_file=None,
        llm_base_url=base_url,
        llm_model="mock-model",
        llm_api_key="mock-key",
    )


async def test_loop_calls_one_tool(mock_base_url: str) -> None:
    async with LLMClient(make_settings(mock_base_url)) as client:
        result = await run_tool_loop(
            client, build_default_registry(), "NB-1024 这台设备的资产信息帮我查一下"
        )

    assert result.called_tools == ["query_asset"]
    assert result.rounds == 2
    assert "张伟" in result.answer
    assert result.cost_cny > 0


async def test_loop_calls_two_tools_in_one_round(mock_base_url: str) -> None:
    """验收点：一句话里并列调用两个工具。"""
    async with LLMClient(make_settings(mock_base_url)) as client:
        result = await run_tool_loop(
            client,
            build_default_registry(),
            "NB-1024 这台电脑是谁在用，他有 VPN 权限吗？",
        )

    assert result.called_tools == ["query_asset", "query_permission"]
    assert result.rounds == 2, "两个工具应在同一轮并行发出，而不是串两轮"
    assert all(record.result.ok for record in result.calls)
    assert len({record.call_id for record in result.calls}) == 2, "每次调用要有独立 id"


async def test_loop_without_tools_answers_directly(mock_base_url: str) -> None:
    async with LLMClient(make_settings(mock_base_url)) as client:
        result = await run_tool_loop(client, build_default_registry(), "你好，你能做什么？")

    assert result.called_tools == []
    assert result.rounds == 1
    assert result.answer


async def test_loop_stops_at_max_rounds(mock_base_url: str) -> None:
    """max_rounds=1 时，第一轮就要了工具 → 没有机会给最终回答 → 抛错。"""
    async with LLMClient(make_settings(mock_base_url)) as client:
        with pytest.raises(FatalError) as excinfo:
            await run_tool_loop(
                client,
                build_default_registry(),
                "NB-1024 这台设备的资产信息帮我查一下",
                max_rounds=1,
            )
    assert "1 轮" in str(excinfo.value)


async def test_summarize_reports_failures(mock_base_url: str) -> None:
    registry = build_default_registry()

    class BrokenParams(BaseModel):
        pass

    async def broken() -> dict:
        raise RuntimeError("坏了")

    registry.register(name="broken", description="坏工具", params_model=BrokenParams)(broken)

    async with LLMClient(make_settings(mock_base_url)) as client:
        result = await run_tool_loop(client, registry, "NB-1024 的资产信息")

    summary = summarize(result)
    assert summary["tools"] == ["query_asset"]
    assert summary["failed"] == []
    assert summary["rounds"] == 2
