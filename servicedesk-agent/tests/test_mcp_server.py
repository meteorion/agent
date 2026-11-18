# file: tests/test_mcp_server.py
"""2.4 的断言：discover、三原语、错误码、日志级别、stdout 纯净。"""

from __future__ import annotations

import io
import json

import pytest

from servicedesk.mcp.envelope import build_request
from servicedesk.mcp.server import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    McpServer,
    serve_stdio,
)


@pytest.fixture()
def server() -> McpServer:
    return McpServer(log_stream=io.StringIO())


async def call(server: McpServer, method: str, params: dict | None = None, **kwargs) -> dict:
    request = build_request(method, params, **kwargs)
    response = await server.handle_async(request)
    assert response is not None
    return response


# ------------------------------ discover ------------------------------


async def test_discover_reports_versions_capabilities_identity(server: McpServer) -> None:
    response = await call(server, "server/discover")
    result = response["result"]

    assert result["resultType"] == "complete"
    assert result["protocolVersions"] == ["2026-07-28"]
    assert result["serverInfo"]["name"] == "servicedesk-mcp"
    assert set(result["capabilities"]) == {"tools", "resources", "prompts"}


# ------------------------------ tools ------------------------------


async def test_tools_list_is_sorted_and_cacheable(server: McpServer) -> None:
    response = await call(server, "tools/list")
    result = response["result"]

    names = [tool["name"] for tool in result["tools"]]
    assert names == sorted(names) == [
        "create_ticket",
        "query_asset",
        "query_permission",
        "query_ticket",
        "search_knowledge",
    ]
    assert result["ttlMs"] > 0
    assert result["cacheScope"] == "global"


async def test_tools_list_is_deterministic(server: McpServer) -> None:
    """顺序稳定才有利于 prompt cache。"""
    first = await call(server, "tools/list", request_id=1)
    second = await call(server, "tools/list", request_id=2)
    assert json.dumps(first["result"], sort_keys=True) == json.dumps(
        second["result"], sort_keys=True
    )


async def test_tool_schema_has_no_refs(server: McpServer) -> None:
    result = (await call(server, "tools/list"))["result"]
    for tool in result["tools"]:
        assert "$defs" not in json.dumps(tool["inputSchema"])
        assert "$ref" not in json.dumps(tool["inputSchema"])


async def test_tools_call_success(server: McpServer) -> None:
    response = await call(
        server, "tools/call", {"name": "query_asset", "arguments": {"hostname": "NB-1024"}}
    )
    result = response["result"]

    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    payload = json.loads(result["content"][0]["text"])
    assert payload["owner"].startswith("张伟")


async def test_tools_call_bad_params_is_iserror_not_protocol_error(server: McpServer) -> None:
    """工具失败是 isError=true，协议层仍然是成功响应 —— 两层语义要分开。"""
    response = await call(
        server, "tools/call", {"name": "query_asset", "arguments": {"hostname": "x"}}
    )
    assert "error" not in response
    assert response["result"]["isError"] is True
    assert "参数校验失败" in response["result"]["content"][0]["text"]


async def test_tools_call_unknown_tool(server: McpServer) -> None:
    response = await call(server, "tools/call", {"name": "query_wifi", "arguments": {}})
    assert response["result"]["isError"] is True
    assert "没有名为" in response["result"]["content"][0]["text"]


async def test_tools_call_requires_name(server: McpServer) -> None:
    response = await call(server, "tools/call", {"arguments": {}})
    assert response["error"]["code"] == INVALID_PARAMS


# ------------------------------ resources ------------------------------


async def test_resources_list_and_read(server: McpServer) -> None:
    listing = (await call(server, "resources/list"))["result"]
    assert listing["resources"][0]["uri"] == "doc://vpn-guide"
    assert listing["resources"][0]["mimeType"] == "text/markdown"

    read = (await call(server, "resources/read", {"uri": "doc://vpn-guide"}))["result"]
    content = read["contents"][0]
    assert content["uri"] == "doc://vpn-guide"
    assert "认证失败" in content["text"]


async def test_resources_read_unknown_uri(server: McpServer) -> None:
    response = await call(server, "resources/read", {"uri": "doc://nope"})
    assert response["error"]["code"] == INVALID_PARAMS


# ------------------------------- prompts -------------------------------


async def test_prompts_list_and_get(server: McpServer) -> None:
    listing = (await call(server, "prompts/list"))["result"]
    assert listing["prompts"][0]["name"] == "explain_ticket"

    got = (
        await call(
            server,
            "prompts/get",
            {"name": "explain_ticket", "arguments": {"ticket_id": "T-1001"}},
        )
    )["result"]
    text = got["messages"][0]["content"]["text"]
    assert "T-1001" in text
    assert got["messages"][0]["role"] == "user"


async def test_prompts_get_missing_argument(server: McpServer) -> None:
    response = await call(server, "prompts/get", {"name": "explain_ticket", "arguments": {}})
    assert response["error"]["code"] == INVALID_PARAMS
    assert "ticket_id" in response["error"]["message"]


# ------------------------------ 错误路径 ------------------------------


async def test_unknown_method(server: McpServer) -> None:
    response = await call(server, "tools/nonexistent")
    assert response["error"]["code"] == METHOD_NOT_FOUND


async def test_missing_method(server: McpServer) -> None:
    response = await server.handle_async({"jsonrpc": "2.0", "id": 9})
    assert response is not None
    assert response["error"]["code"] == INVALID_REQUEST


async def test_unsupported_version_lists_alternatives(server: McpServer) -> None:
    # 双栈后 2025-11-25 是合法 legacy；这里用一个真正不支持的版本
    response = await call(server, "tools/list", protocol_version="1999-01-01")
    assert response["error"]["code"] == INVALID_REQUEST
    assert "2026-07-28" in response["error"]["data"]["supported"]


# ------------------------------ 日志级别 ------------------------------


async def test_default_log_level_hides_debug() -> None:
    stream = io.StringIO()
    server = McpServer(log_stream=stream)
    await call(server, "tools/list")
    assert "request_received" not in stream.getvalue()


async def test_request_log_level_enables_debug() -> None:
    stream = io.StringIO()
    server = McpServer(log_stream=stream)
    await call(server, "tools/list", log_level="debug")
    logs = stream.getvalue()
    assert "request_received" in logs
    assert "request_done" in logs


async def test_logs_never_go_to_the_server_output(server: McpServer) -> None:
    """日志写进自己的 log_stream，不会污染协议输出。"""
    stdout = io.StringIO()
    stdin = io.StringIO(
        json.dumps(build_request("tools/call", {"name": "query_ticket", "arguments": {"ticket_id": "T-1001"}}))
        + "\n"
    )
    await serve_stdio(server, stdin=stdin, stdout=stdout)

    lines = stdout.getvalue().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["result"]["isError"] is False


# ------------------------------ stdio 循环 ------------------------------


async def test_stdio_survives_bad_json_and_keeps_serving() -> None:
    stdout = io.StringIO()
    stdin = io.StringIO("这不是 JSON\n" + json.dumps(build_request("server/discover")) + "\n")
    await serve_stdio(McpServer(log_stream=io.StringIO()), stdin=stdin, stdout=stdout)

    lines = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["error"]["code"] == PARSE_ERROR
    assert lines[1]["result"]["resultType"] == "complete"


async def test_stdio_skips_blank_lines() -> None:
    stdout = io.StringIO()
    stdin = io.StringIO("\n\n" + json.dumps(build_request("server/discover")) + "\n\n")
    await serve_stdio(McpServer(log_stream=io.StringIO()), stdin=stdin, stdout=stdout)

    lines = stdout.getvalue().strip().splitlines()
    assert len(lines) == 1
