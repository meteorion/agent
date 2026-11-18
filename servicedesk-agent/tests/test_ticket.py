# file: tests/test_ticket.py
"""1.4 的断言：脏 JSON 必须被"可读地"拦下，而不是崩掉或静默通过。"""

from __future__ import annotations

import json

import pytest

from servicedesk.models.ticket import (
    Category,
    Priority,
    Ticket,
    TicketParseError,
    extract_json_object,
    load_ticket,
    ticket_tool_schema,
)

CLEAN = {
    "ticket_id": "T-1001",
    "title": "VPN 连不上",
    "summary": "从今天早上开始，公司 VPN 一直提示认证失败，重启客户端无效。",
    "priority": "P1",
    "category": "network",
    "affected_system": "VPN 网关",
    "reporter": "张伟",
    "tags": ["vpn", "认证"],
    "confidence": 0.82,
    "needs_approval": False,
}


def dump(**overrides: object) -> str:
    payload = {**CLEAN, **overrides}
    return json.dumps(payload, ensure_ascii=False)


# ------------------------------ 正常路径 ------------------------------


def test_clean_json_parses() -> None:
    ticket = load_ticket(dump())
    assert ticket.ticket_id == "T-1001"
    assert ticket.priority is Priority.P1
    assert ticket.category is Category.NETWORK
    assert ticket.confidence == pytest.approx(0.82)


def test_code_fence_is_stripped() -> None:
    """模型最常见的包裹形式：先说话，再给 ```json 代码块。"""
    raw = "好的，已经整理好了：\n```json\n" + dump() + "\n```\n还需要调整吗？"
    assert load_ticket(raw).ticket_id == "T-1001"


def test_braces_inside_string_do_not_break_extraction() -> None:
    """标题里出现花括号时，括号配对应仍然抠出完整对象。"""
    raw = dump(title="VPN{紧急}连不上")
    extracted = extract_json_object(raw)
    assert extracted is not None
    assert extracted.endswith("}")
    assert load_ticket(raw).title == "VPN{紧急}连不上"


def test_chinese_enum_values_are_normalized() -> None:
    ticket = load_ticket(dump(priority="紧急", category="软件", needs_approval=True))
    assert ticket.priority is Priority.P0
    assert ticket.category is Category.SOFTWARE


def test_comma_separated_tags_become_list() -> None:
    ticket = load_ticket(dump(tags="白屏、财务，紧急"))
    assert ticket.tags == ["白屏", "财务", "紧急"]


def test_string_number_is_coerced_in_lax_mode() -> None:
    """pydantic 默认宽松模式会把 "0.9" 转成 0.9。

    这不是 bug，但你得知道：宽松会掩盖"模型输出格式不稳"这个信号。
    """
    ticket = load_ticket(dump(confidence="0.9"))
    assert ticket.confidence == pytest.approx(0.9)
    assert isinstance(ticket.confidence, float)


# ------------------------------ 失败路径 ------------------------------


def test_extra_field_is_rejected() -> None:
    with pytest.raises(TicketParseError) as excinfo:
        load_ticket(dump(severity="high"))

    error = excinfo.value
    assert error.stage == "invalid"
    assert any(e["type"] == "extra_forbidden" for e in error.errors)
    assert "severity" in error.repair_message


def test_missing_field_repair_says_not_provided() -> None:
    payload = {k: v for k, v in CLEAN.items() if k != "summary"}
    with pytest.raises(TicketParseError) as excinfo:
        load_ticket(json.dumps(payload, ensure_ascii=False))

    message = excinfo.value.repair_message
    assert "整体对象" not in message
    assert "（未提供）" in message
    assert "summary" in message


def test_p0_requires_approval() -> None:
    with pytest.raises(TicketParseError) as excinfo:
        load_ticket(dump(priority="P0", needs_approval=False))

    message = excinfo.value.repair_message
    assert "needs_approval" in message
    assert "Value error, " not in message, "pydantic 的前缀应该被去掉"


def test_plain_text_raises_not_json() -> None:
    with pytest.raises(TicketParseError) as excinfo:
        load_ticket("我理解你的 VPN 连不上，建议先检查网络。")

    assert excinfo.value.stage == "not_json"
    assert "JSON" in excinfo.value.repair_message


def test_out_of_range_confidence_is_caught() -> None:
    with pytest.raises(TicketParseError) as excinfo:
        load_ticket(dump(confidence=1.7))
    assert "confidence" in excinfo.value.repair_message


# -------------------------------- schema --------------------------------


def test_tool_schema_matches_model() -> None:
    tool = ticket_tool_schema()
    parameters = tool["function"]["parameters"]
    required = set(parameters["required"])

    assert tool["function"]["name"] == "create_ticket"
    assert {"ticket_id", "title", "summary", "priority", "category"} <= required
    # 有默认值的字段不应出现在 required 里
    assert "needs_approval" not in required
    assert "tags" not in required
    assert set(parameters["$defs"]) == {"Priority", "Category"}


def test_model_forbids_unknown_fields_directly() -> None:
    with pytest.raises(Exception):
        Ticket.model_validate({**CLEAN, "unknown_key": 1})
