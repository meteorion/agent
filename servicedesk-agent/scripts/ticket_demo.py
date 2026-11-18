# file: scripts/ticket_demo.py
"""1.4 的场景：模型会怎么把 JSON 写错，以及我们怎么接住。

不联网、不花钱——所有"模型的输出"都是内置的字符串常量，
但它们都是从真实 LLM 输出里归纳出来的典型脏数据。

用法：
    uv run python -m scripts.ticket_demo
"""

from __future__ import annotations

import json

from servicedesk.models.ticket import (
    Ticket,
    TicketParseError,
    load_ticket,
    pretty_schema_summary,
    ticket_tool_schema,
)

CLEAN = json.dumps(
    {
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
        "created_at": "2026-11-17T09:30:00+08:00",
    },
    ensure_ascii=False,
)

CASES: list[tuple[str, str]] = [
    ("干净的 JSON", CLEAN),
    (
        "markdown 代码块包裹（最常见）",
        "好的，我已经整理好了：\n```json\n" + CLEAN + "\n```\n如需调整请告诉我。",
    ),
    (
        "中文口语化枚举：紧急 / 网络 / 逗号分隔标签",
        json.dumps(
            {
                "ticket_id": "T-1002",
                "title": "财务系统打不开",
                "summary": "财务共享系统页面一直白屏，整个部门都无法使用。",
                "priority": "紧急",
                "category": "软件",
                "affected_system": "财务共享",
                "reporter": "李娜",
                "tags": "白屏、财务、紧急",
                "confidence": 0.91,
                "needs_approval": True,
            },
            ensure_ascii=False,
        ),
    ),
    (
        "数字被写成字符串：confidence = \"0.9\"",
        CLEAN.replace('"confidence": 0.82', '"confidence": "0.9"'),
    ),
    (
        "模型自己加了个字段 severity",
        json.dumps({**json.loads(CLEAN), "severity": "high"}, ensure_ascii=False),
    ),
    (
        "漏了必填字段 summary",
        json.dumps({k: v for k, v in json.loads(CLEAN).items() if k != "summary"}, ensure_ascii=False),
    ),
    (
        "违反跨字段规则：P0 却没标 needs_approval",
        json.dumps({**json.loads(CLEAN), "priority": "P0"}, ensure_ascii=False),
    ),
    (
        "干脆不是 JSON，只是一段自然语言",
        "我理解你的 VPN 连不上，建议先检查网络，再重启客户端。",
    ),
]


def header(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def show_case(index: int, name: str, raw: str) -> bool:
    """返回 True 表示解析成功。"""
    header(f"案例 {index}：{name}")
    preview = raw.replace("\n", "\\n")
    print(f"输入: {preview[:96]}{'…' if len(preview) > 96 else ''}")
    print("-" * 68)

    try:
        ticket = load_ticket(raw)
    except TicketParseError as exc:
        print(f"结果: ❌ 被拦下（stage={exc.stage}，{len(exc.errors)} 处问题）")
        print("修复提示（可直接作为下一条 user 消息回喂给模型）:")
        for line in exc.repair_message.splitlines():
            print(f"  {line}")
        return False

    print("结果: ✅ 解析成功")
    print(f"  ticket_id  : {ticket.ticket_id}")
    print(f"  priority   : {ticket.priority.value}    category: {ticket.category.value}")
    print(f"  tags       : {ticket.tags}")
    print(f"  confidence : {ticket.confidence} ({type(ticket.confidence).__name__})")
    print(f"  approval   : {ticket.needs_approval}")
    print(f"  created_at : {ticket.created_at}")
    return True


def main() -> int:
    header("第 0 步：schema 是从模型自动生成的")
    print(pretty_schema_summary())
    tool = ticket_tool_schema()
    print(f"\n函数名: {tool['function']['name']}")
    print(f"必填字段: {tool['function']['parameters'].get('required')}")

    ok = 0
    for index, (name, raw) in enumerate(CASES, start=1):
        if show_case(index, name, raw):
            ok += 1

    header("小结")
    print(f"  {len(CASES)} 个案例：{ok} 个成功、{len(CASES) - ok} 个被拦下并给出可回喂的修复提示。")
    print("  关键点：校验失败不是崩溃，而是「让模型重说一次」的机会。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
