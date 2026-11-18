# file: tests/test_mcp_auth_mrtr.py
"""2.6 的断言：401/403 分得清、MRTR 能往返、头是 ASCII。"""

from __future__ import annotations

import json

import httpx
import pytest

from servicedesk.mcp.auth import (
    SCOPE_APPROVE,
    SCOPE_READ,
    SCOPE_WRITE,
    AuthError,
    InsufficientScope,
    TokenStore,
    check_scope,
    dynamic_scopes,
    parse_bearer,
)
from servicedesk.mcp.envelope import PROTOCOL_VERSION, build_request, build_retry_with_inputs
from servicedesk.mcp.http_app import create_server
from servicedesk.mcp.server import McpServer


def headers(method: str, name: str | None = None, token: str | None = None) -> dict[str, str]:
    result = {
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name:
        result["Mcp-Name"] = name
    if token:
        result["Authorization"] = f"Bearer {token}"
    return result


# -------------------------------- 令牌 --------------------------------


def test_parse_bearer_ok() -> None:
    assert parse_bearer("Bearer abc123") == "abc123"
    assert parse_bearer("bearer abc123") == "abc123"


@pytest.mark.parametrize("value", [None, "", "abc123", "Basic abc"])
def test_parse_bearer_rejects_bad_input(value: str | None) -> None:
    with pytest.raises(AuthError):
        parse_bearer(value)


def test_token_roundtrip_and_scopes() -> None:
    store = TokenStore()
    token = store.issue("alice", {SCOPE_READ})
    claims = store.verify(token)
    assert claims.subject == "alice"
    assert claims.scopes == frozenset({SCOPE_READ})


def test_expired_token_is_rejected() -> None:
    store = TokenStore()
    token = store.issue("alice", {SCOPE_READ}, ttl_s=-1)
    with pytest.raises(AuthError) as excinfo:
        store.verify(token)
    assert excinfo.value.error_code == "expired_token"


def test_unknown_scope_rejected_at_issue() -> None:
    with pytest.raises(ValueError):
        TokenStore().issue("alice", {"root:everything"})


# -------------------------------- scope --------------------------------


def test_check_scope_allows_and_denies() -> None:
    store = TokenStore()
    read_claims = store.verify(store.issue("alice", {SCOPE_READ}))
    write_claims = store.verify(store.issue("bob", {SCOPE_READ, SCOPE_WRITE}))

    check_scope(read_claims, "query_asset")          # 不抛
    with pytest.raises(InsufficientScope):
        check_scope(read_claims, "create_ticket")
    check_scope(write_claims, "create_ticket")       # 不抛


def test_p0_needs_approve_scope() -> None:
    """同一个工具，参数不同 → 需要的权限不同。"""
    assert dynamic_scopes("create_ticket", {"priority": "P0"}) == frozenset({SCOPE_APPROVE})
    assert dynamic_scopes("create_ticket", {"priority": "P3"}) == frozenset()

    store = TokenStore()
    write_claims = store.verify(store.issue("bob", {SCOPE_READ, SCOPE_WRITE}))
    check_scope(write_claims, "create_ticket", {"priority": "P3"})
    with pytest.raises(InsufficientScope):
        check_scope(write_claims, "create_ticket", {"priority": "P0"})


def test_www_authenticate_is_ascii() -> None:
    """回归：把中文塞进 HTTP 头会让 send_header 抛 UnicodeEncodeError。"""
    error = AuthError("缺少 Authorization 头", error_code="missing_token")
    assert error.www_authenticate.isascii()

    insufficient = InsufficientScope("create_ticket", frozenset({SCOPE_WRITE}), frozenset({SCOPE_READ}))
    assert insufficient.www_authenticate.isascii()
    assert 'scope="tools:write"' in insufficient.www_authenticate


def test_insufficient_scope_payload() -> None:
    exc = InsufficientScope("create_ticket", frozenset({SCOPE_WRITE}), frozenset({SCOPE_READ}))
    assert exc.payload["required"] == [SCOPE_WRITE]
    assert exc.payload["granted"] == [SCOPE_READ]
    assert exc.status == 403


# -------------------------------- MRTR --------------------------------


async def test_tool_returns_input_required() -> None:
    server = McpServer()
    response = await server.handle_async(
        build_request("tools/call", {"name": "create_ticket", "arguments": {"title": "VPN 连不上"}})
    )
    result = response["result"]

    assert result["resultType"] == "input_required"
    ids = [item["id"] for item in result["inputRequests"]]
    assert ids == ["priority", "approver"]
    assert result["inputRequests"][0]["schema"]["enum"] == ["P0", "P1", "P2", "P3"]


async def test_retry_with_input_responses_completes() -> None:
    server = McpServer()
    first = build_request(
        "tools/call", {"name": "create_ticket", "arguments": {"title": "VPN 连不上"}}, request_id=7
    )
    await server.handle_async(first)

    retried = build_retry_with_inputs(first, {"priority": "P1", "approver": "E-2048"})
    response = await server.handle_async(retried)
    result = response["result"]

    assert result["resultType"] == "complete"
    assert result["isError"] is False
    created = json.loads(result["content"][0]["text"])
    assert created["priority"] == "P1"
    assert created["ticket_id"].startswith("T-")


async def test_retry_is_idempotent() -> None:
    """同样答案重试 → 同一个工单号，不会重复建单。"""
    server = McpServer()
    body = build_request(
        "tools/call", {"name": "create_ticket", "arguments": {"title": "VPN 连不上"}}, request_id=8
    )
    retried = build_retry_with_inputs(body, {"priority": "P1", "approver": "E-2048"})

    first = await server.handle_async(retried)
    second = await server.handle_async(retried)

    id_a = json.loads(first["result"]["content"][0]["text"])["ticket_id"]
    id_b = json.loads(second["result"]["content"][0]["text"])["ticket_id"]
    assert id_a == id_b


# ------------------------------ HTTP 鉴权 ------------------------------


@pytest.fixture()
def secured():
    tokens = TokenStore()
    read_token = tokens.issue("alice", {SCOPE_READ})
    write_token = tokens.issue("bob", {SCOPE_READ, SCOPE_WRITE})
    admin_token = tokens.issue("carol", {SCOPE_READ, SCOPE_WRITE, SCOPE_APPROVE})
    server, url = create_server(tokens=tokens)
    yield url, read_token, write_token, admin_token
    server.shutdown()


async def test_missing_token_gets_401(secured) -> None:
    url, *_ = secured
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url, json=build_request("tools/list"), headers=headers("tools/list")
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_token"
    assert response.headers["www-authenticate"].isascii()


async def test_read_scope_cannot_create_ticket(secured) -> None:
    url, read_token, *_ = secured
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            json=build_request("tools/call", {"name": "create_ticket", "arguments": {"title": "VPN 连不上"}}),
            headers=headers("tools/call", "create_ticket", read_token),
        )
    assert response.status_code == 403
    assert response.json()["error"]["required"] == [SCOPE_WRITE]


async def test_p0_requires_approve_scope(secured) -> None:
    url, _, write_token, admin_token = secured
    body = build_request(
        "tools/call",
        {"name": "create_ticket",
         "arguments": {"title": "核心交换机故障", "priority": "P0", "approver": "E-2048"}},
    )
    async with httpx.AsyncClient() as client:
        denied = await client.post(
            url, json=body, headers=headers("tools/call", "create_ticket", write_token)
        )
        allowed = await client.post(
            url, json=body, headers=headers("tools/call", "create_ticket", admin_token)
        )

    assert denied.status_code == 403
    assert SCOPE_APPROVE in denied.json()["error"]["required"]
    assert allowed.status_code == 200
    assert allowed.json()["result"]["resultType"] == "complete"


async def test_expired_token_gets_401_with_code() -> None:
    tokens = TokenStore()
    expired = tokens.issue("dave", {SCOPE_READ}, ttl_s=-1)
    server, url = create_server(tokens=tokens)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url, json=build_request("tools/list"), headers=headers("tools/list", token=expired)
            )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "expired_token"
    finally:
        server.shutdown()


async def test_mrtr_roundtrip_over_http(secured) -> None:
    """缺参数走 200 + input_required，补答案后成功——不是 4xx。"""
    url, _, write_token, _ = secured
    first = build_request(
        "tools/call", {"name": "create_ticket", "arguments": {"title": "VPN 连接后立刻断开"}},
        request_id=42,
    )
    async with httpx.AsyncClient() as client:
        ask = await client.post(
            url, json=first, headers=headers("tools/call", "create_ticket", write_token)
        )
        assert ask.status_code == 200
        assert ask.json()["result"]["resultType"] == "input_required"

        retry = build_retry_with_inputs(first, {"priority": "P2", "approver": "E-2048"})
        done = await client.post(
            url, json=retry, headers=headers("tools/call", "create_ticket", write_token)
        )

    assert done.status_code == 200
    result = done.json()["result"]
    assert result["resultType"] == "complete"
    assert json.loads(result["content"][0]["text"])["priority"] == "P2"
