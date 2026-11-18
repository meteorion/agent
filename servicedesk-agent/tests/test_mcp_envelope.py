# file: tests/test_mcp_envelope.py
"""2.3 的断言：信封、版本协商、resultType 兼容、MRTR、显式句柄。"""

from __future__ import annotations

import json

import pytest

from servicedesk.mcp.envelope import (
    LEGACY_PROTOCOL_VERSION,
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_LOG_LEVEL,
    META_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    HandleStore,
    UnsupportedProtocolVersionError,
    build_headers,
    build_request,
    build_retry_with_inputs,
    check_protocol_version,
    input_requests,
    is_input_required,
    result_type,
    supported_versions,
)


# ------------------------------- 请求信封 -------------------------------


def test_request_carries_identity_and_version_in_meta() -> None:
    request = build_request("tools/call", {"name": "query_asset"}, request_id=1)

    assert request["jsonrpc"] == "2.0"
    assert request["id"] == 1
    assert request["method"] == "tools/call"
    assert request["params"]["name"] == "query_asset"

    meta = request["params"]["_meta"]
    assert meta[META_PROTOCOL_VERSION] == PROTOCOL_VERSION
    assert meta[META_CLIENT_INFO]["name"] == "servicedesk-agent"


def test_request_omits_optional_meta_when_not_needed() -> None:
    meta = build_request("tools/list")["params"]["_meta"]
    assert META_CLIENT_CAPABILITIES not in meta
    assert META_LOG_LEVEL not in meta
    assert "traceparent" not in meta


def test_request_includes_optional_meta_when_given() -> None:
    meta = build_request(
        "tools/list",
        client_capabilities={"elicitation": {}},
        log_level="debug",
        traceparent="00-aaa-bbb-01",
    )["params"]["_meta"]

    assert meta[META_CLIENT_CAPABILITIES] == {"elicitation": {}}
    assert meta[META_LOG_LEVEL] == "debug"
    assert meta["traceparent"] == "00-aaa-bbb-01"


def test_request_ids_are_unique_by_default() -> None:
    first = build_request("tools/list")["id"]
    second = build_request("tools/list")["id"]
    assert first != second


def test_headers_include_routing_fields() -> None:
    headers = build_headers("tools/call", name="query_asset")
    assert headers["Mcp-Method"] == "tools/call"
    assert headers["Mcp-Name"] == "query_asset"
    assert headers["MCP-Protocol-Version"] == PROTOCOL_VERSION


def test_headers_omit_name_when_not_applicable() -> None:
    headers = build_headers("tools/list")
    assert "Mcp-Name" not in headers


# ------------------------------- 版本协商 -------------------------------


def test_supported_version_is_accepted() -> None:
    assert check_protocol_version(PROTOCOL_VERSION, [PROTOCOL_VERSION]) == PROTOCOL_VERSION


def test_unsupported_version_lists_alternatives() -> None:
    with pytest.raises(UnsupportedProtocolVersionError) as excinfo:
        check_protocol_version(LEGACY_PROTOCOL_VERSION, [PROTOCOL_VERSION])

    error = excinfo.value
    assert error.requested == LEGACY_PROTOCOL_VERSION
    assert error.supported == [PROTOCOL_VERSION]
    assert PROTOCOL_VERSION in str(error)


def test_supported_versions_are_sorted_and_unique() -> None:
    with pytest.raises(UnsupportedProtocolVersionError) as excinfo:
        check_protocol_version("1999-01-01", ["2026-07-28", "2025-11-25", "2026-07-28"])
    assert excinfo.value.supported == ["2025-11-25", "2026-07-28"]


def test_supported_versions_tolerates_unknown_shape() -> None:
    assert supported_versions({"protocolVersions": ["2026-07-28"]}) == ["2026-07-28"]
    assert supported_versions({"supportedProtocolVersions": ["x"]}) == ["x"]
    assert supported_versions({"unexpected": 1}) == []


# ----------------------------- resultType -----------------------------


def test_result_type_defaults_to_complete_for_legacy_server() -> None:
    """旧版 Server 不带 resultType，必须按 complete 处理。"""
    assert result_type({"result": {"content": []}}) == "complete"
    assert result_type({"result": {"resultType": "complete"}}) == "complete"


def test_result_type_recognizes_input_required() -> None:
    response = {"result": {"resultType": "input_required", "inputRequests": []}}
    assert result_type(response) == "input_required"
    assert is_input_required(response) is True


def test_unknown_result_type_falls_back_to_complete() -> None:
    assert result_type({"result": {"resultType": "something_new"}}) == "complete"


def test_missing_result_is_complete() -> None:
    assert result_type({"error": {"code": -32602, "message": "bad"}}) == "complete"


# -------------------------------- MRTR --------------------------------


def test_input_requests_are_exposed() -> None:
    response = {
        "result": {
            "resultType": "input_required",
            "inputRequests": [{"id": "prio", "prompt": "优先级？"}],
        }
    }
    assert input_requests(response)[0]["id"] == "prio"
    assert input_requests({"result": {}}) == []


def test_retry_keeps_original_request_identity() -> None:
    original = build_request("tools/call", {"name": "create_ticket"}, request_id=7)
    retried = build_retry_with_inputs(original, {"prio": "P1"})

    assert retried["id"] == original["id"]
    assert retried["method"] == original["method"]
    assert retried["params"]["name"] == "create_ticket"
    assert retried["params"]["_meta"] == original["params"]["_meta"]
    assert retried["params"]["inputResponses"] == {"prio": "P1"}


def test_retry_does_not_mutate_original() -> None:
    original = build_request("tools/call", {"name": "create_ticket"}, request_id=8)
    before = json.dumps(original, sort_keys=True)
    build_retry_with_inputs(original, {"prio": "P1"})
    assert json.dumps(original, sort_keys=True) == before


# ------------------------------ 显式句柄 ------------------------------


def test_handle_roundtrip() -> None:
    store = HandleStore(ttl_s=60)
    handle = store.mint({"user": "张伟", "step": 1})

    assert handle.startswith("h_")
    assert store.resolve(handle) == {"user": "张伟", "step": 1}
    assert store.resolve("h_missing") is None


def test_handle_expires() -> None:
    store = HandleStore(ttl_s=-1)          # 立刻过期
    handle = store.mint({"user": "张伟"})
    assert store.resolve(handle) is None
    assert len(store) == 0, "过期记录应被清理"


def test_handles_are_isolated() -> None:
    store = HandleStore()
    first = store.mint({"step": 1})
    second = store.mint({"step": 2})
    assert store.resolve(first) == {"step": 1}
    assert store.resolve(second) == {"step": 2}
