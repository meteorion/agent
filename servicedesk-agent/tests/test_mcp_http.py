# file: tests/test_mcp_http.py
"""2.5 的断言：单端点、路由头、订阅流、断流重发、两实例互不影响。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from servicedesk.mcp.envelope import PROTOCOL_VERSION, HandleStore, build_request
from servicedesk.mcp.http_app import create_server


def headers(method: str, name: str | None = None, version: str = PROTOCOL_VERSION) -> dict[str, str]:
    result = {
        "Content-Type": "application/json",
        "MCP-Protocol-Version": version,
        "Mcp-Method": method,
    }
    if name:
        result["Mcp-Name"] = name
    return result


@pytest.fixture()
def server_a():
    server, url = create_server()
    yield url
    server.shutdown()


@pytest.fixture()
def server_b():
    server, url = create_server()
    yield url
    server.shutdown()


# ------------------------------ 单端点 ------------------------------


async def test_discover_over_http(server_a: str) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            server_a,
            json=build_request("server/discover", request_id=1),
            headers=headers("server/discover"),
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["resultType"] == "complete"
    assert result["protocolVersions"] == [PROTOCOL_VERSION]


async def test_unknown_path_returns_404(server_a: str) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            server_a.replace("/mcp", "/other"),
            json=build_request("tools/list"),
            headers=headers("tools/list"),
        )
    assert response.status_code == 404


# ------------------------------ 路由头 ------------------------------


async def test_missing_mcp_method_header_is_rejected(server_a: str) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            server_a,
            json=build_request("tools/list"),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert "Mcp-Method" in response.json()["error"]["message"]


async def test_header_body_method_mismatch_is_rejected(server_a: str) -> None:
    """否则可以用一个合法头把请求偷运到别的处理器。"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            server_a,
            json=build_request("tools/call", {"name": "query_ticket", "arguments": {}}),
            headers=headers("tools/list"),
        )
    assert response.status_code == 400
    assert "不一致" in response.json()["error"]["message"]


async def test_name_header_must_match_arguments(server_a: str) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            server_a,
            json=build_request("tools/call", {"name": "query_ticket", "arguments": {}}),
            headers=headers("tools/call", "query_asset"),
        )
    assert response.status_code == 400


async def test_unsupported_version_header_is_rejected(server_a: str) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            server_a,
            json=build_request("tools/list"),
            headers=headers("tools/list", version="1999-01-01"),
        )
    assert response.status_code == 400
    assert PROTOCOL_VERSION in response.json()["error"]["data"]["supported"]


# ------------------------------ tools/call ------------------------------


async def test_tools_call_over_http(server_a: str) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            server_a,
            json=build_request(
                "tools/call",
                {"name": "query_asset", "arguments": {"hostname": "NB-1024"}},
            ),
            headers=headers("tools/call", "query_asset"),
        )
    result = response.json()["result"]
    assert result["isError"] is False
    assert "张伟" in result["content"][0]["text"]


async def test_tool_error_is_iserror_not_http_error(server_a: str) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            server_a,
            json=build_request(
                "tools/call", {"name": "query_asset", "arguments": {"hostname": "x"}}
            ),
            headers=headers("tools/call", "query_asset"),
        )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True


async def test_bad_json_returns_parse_error(server_a: str) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            server_a, content=b"{not json", headers=headers("tools/list")
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700


# ------------------------------ 订阅流 ------------------------------


async def read_events(
    client: httpx.AsyncClient, url: str, body: dict, limit: int = 4
) -> list[dict]:
    events: list[dict] = []
    async with client.stream(
        "POST", url, json=body, headers=headers("subscriptions/listen"), timeout=10.0
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            events.append(json.loads(line[5:].strip()))
            if len(events) >= limit:
                break
    return events


async def test_subscription_ack_and_notifications(server_a: str) -> None:
    body = build_request(
        "subscriptions/listen", {"types": ["toolsListChanged"]}, request_id=11
    )
    async with httpx.AsyncClient() as client:
        events = await read_events(client, server_a, body, limit=3)

    ack = events[0]["result"]
    assert ack["subscriptionId"].startswith("sub_")
    assert ack["types"] == ["toolsListChanged"]

    notification = events[1]
    assert notification["method"] == "notifications/toolsListChanged"
    assert (
        notification["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"]
        == ack["subscriptionId"]
    )


async def test_unknown_subscription_type_is_rejected_not_fatal(server_a: str) -> None:
    body = build_request(
        "subscriptions/listen", {"types": ["bogusType", "toolsListChanged"]}, request_id=12
    )
    async with httpx.AsyncClient() as client:
        events = await read_events(client, server_a, body, limit=2)

    ack = events[0]["result"]
    assert ack["rejected"] == ["bogusType"]
    assert ack["types"] == ["toolsListChanged"]


async def test_resubscribe_uses_new_request_id_and_new_subscription(server_a: str) -> None:
    """流断了就是丢了：只能换新 id 重新订阅，不能续传。"""
    first = build_request("subscriptions/listen", {"types": ["toolsListChanged"]}, request_id=20)
    second = build_request("subscriptions/listen", {"types": ["toolsListChanged"]}, request_id=21)

    async with httpx.AsyncClient() as client:
        events_a = await read_events(client, server_a, first, limit=1)
        events_b = await read_events(client, server_a, second, limit=1)

    assert first["id"] != second["id"]
    assert events_a[0]["result"]["subscriptionId"] != events_b[0]["result"]["subscriptionId"]


# ---------------------------- 水平扩容 ----------------------------


async def test_two_instances_serve_concurrently(server_a: str, server_b: str) -> None:
    """两个并发请求打到不同实例，都成功且互不影响。"""
    payloads = [(server_a, "NB-1024"), (server_b, "NB-2077")]

    async with httpx.AsyncClient() as client:
        responses = await asyncio.gather(
            *[
                client.post(
                    url,
                    json=build_request(
                        "tools/call",
                        {"name": "query_asset", "arguments": {"hostname": hostname}},
                    ),
                    headers=headers("tools/call", "query_asset"),
                )
                for url, hostname in payloads
            ]
        )

    owners = []
    for response in responses:
        result = response.json()["result"]
        assert result["isError"] is False
        owners.append(json.loads(result["content"][0]["text"])["owner"])

    assert owners[0].startswith("张伟")
    assert owners[1].startswith("李娜")


async def test_instances_share_no_session_state(server_a: str, server_b: str) -> None:
    """第一个实例处理过的请求，不影响第二个实例——因为它根本没记住。"""
    async with httpx.AsyncClient() as client:
        first = await client.post(
            server_a,
            json=build_request("tools/call", {"name": "query_ticket", "arguments": {"ticket_id": "T-1001"}}),
            headers=headers("tools/call", "query_ticket"),
        )
        second = await client.post(
            server_b,
            json=build_request("tools/list"),
            headers=headers("tools/list"),
        )
    assert first.json()["result"]["isError"] is False
    assert len(second.json()["result"]["tools"]) == 5


def test_handle_store_is_transport_agnostic() -> None:
    """跨调用状态在 store 里，不在实例里，所以任意实例都能接着处理。"""
    store = HandleStore()
    handle = store.mint({"user": "张伟", "step": 2})
    assert store.resolve(handle) == {"user": "张伟", "step": 2}
