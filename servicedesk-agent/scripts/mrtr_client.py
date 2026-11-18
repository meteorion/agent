# file: scripts/mrtr_client.py
"""2.6 的场景：缺权限（403）与缺参数（input_required）是两件事。

用法：
    uv run python -m scripts.mrtr_client
"""

from __future__ import annotations

import asyncio
import json

import httpx

from servicedesk.mcp.auth import SCOPE_APPROVE, SCOPE_READ, SCOPE_WRITE, TokenStore
from servicedesk.mcp.envelope import PROTOCOL_VERSION, build_request, build_retry_with_inputs
from servicedesk.mcp.http_app import create_server
from servicedesk.logging_setup import configure_logging


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def req_headers(method: str, name: str | None = None, token: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name:
        headers["Mcp-Name"] = name
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def main() -> int:
    configure_logging(level="ERROR")

    tokens = TokenStore()
    read_token = tokens.issue("alice", {SCOPE_READ}, ttl_s=600)
    write_token = tokens.issue("bob", {SCOPE_READ, SCOPE_WRITE}, ttl_s=600)
    admin_token = tokens.issue("carol", {SCOPE_READ, SCOPE_WRITE, SCOPE_APPROVE}, ttl_s=600)

    server, url = create_server(tokens=tokens)
    print(f"服务端: {url}")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            header("第 1 步：没有 token → 401，而不是 403")
            no_token = await client.post(
                url, json=build_request("tools/list", request_id=1), headers=req_headers("tools/list")
            )
            print(f"  status={no_token.status_code}")
            print(f"  WWW-Authenticate: {no_token.headers.get('www-authenticate')}")
            print(f"  body: {no_token.json()['error']}")

            header("第 2 步：有读权限 → 查询类工具正常")
            read_ok = await client.post(
                url,
                json=build_request(
                    "tools/call",
                    {"name": "query_asset", "arguments": {"hostname": "NB-1024"}},
                    request_id=2,
                ),
                headers=req_headers("tools/call", "query_asset", read_token),
            )
            result = read_ok.json()["result"]
            print(f"  status={read_ok.status_code}  isError={result['isError']}")
            print(f"  内容: {result['content'][0]['text'][:90]}")

            header("第 3 步：只有读权限去建单 → 403 insufficient_scope")
            denied = await client.post(
                url,
                json=build_request(
                    "tools/call", {"name": "create_ticket", "arguments": {"title": "VPN 连不上"}}, request_id=3
                ),
                headers=req_headers("tools/call", "create_ticket", read_token),
            )
            print(f"  status={denied.status_code}")
            print(f"  WWW-Authenticate: {denied.headers.get('www-authenticate')}")
            print(f"  body: {json.dumps(denied.json()['error'], ensure_ascii=False)}")
            print("  >>> 这条不能靠'问用户'解决：用户再描述一遍需求也拿不到 tools:write。")

            header("第 4 步：有写权限但没给全参数 → 200 + input_required")
            first = build_request(
                "tools/call",
                {"name": "create_ticket", "arguments": {"title": "VPN 连接后立刻断开"}},
                request_id=4,
            )
            ask = await client.post(
                url, json=first, headers=req_headers("tools/call", "create_ticket", write_token)
            )
            payload = ask.json()["result"]
            print(f"  status={ask.status_code}  resultType={payload['resultType']}")
            for item in payload.get("inputRequests", []):
                print(f"    - [{item['id']}] {item['prompt']}")
                print(f"      schema={item.get('schema')}")
            print("  >>> 注意 HTTP 是 200：这不是失败，是'我还需要两样东西'。")

            header("第 5 步：客户端补上答案，重试**原请求**")
            answers = {"priority": "P1", "approver": "E-2048"}
            retry = build_retry_with_inputs(first, answers)
            print(f"  重试用的 id={retry['id']}（与首次相同：{retry['id'] == first['id']}）")
            done = await client.post(
                url, json=retry, headers=req_headers("tools/call", "create_ticket", write_token)
            )
            completed = done.json()["result"]
            print(f"  status={done.status_code}  resultType={completed['resultType']}")
            created = json.loads(completed["content"][0]["text"])
            print(f"  工单: {json.dumps(created, ensure_ascii=False)}")

            header("第 6 步：同样答案再重试一次 → 工单号相同（幂等）")
            again = await client.post(
                url, json=build_retry_with_inputs(first, answers),
                headers=req_headers("tools/call", "create_ticket", write_token),
            )
            created_again = json.loads(again.json()["result"]["content"][0]["text"])
            same = created_again["ticket_id"] == created["ticket_id"]
            print(f"  第一次: {created['ticket_id']}   第二次: {created_again['ticket_id']}")
            print(f"  是否相同: {same}   ← 幂等键 = title + priority")

            header("第 7 步：同一个工具，参数不同 → 需要的权限也不同")
            p0_body = build_request(
                "tools/call",
                {"name": "create_ticket",
                 "arguments": {"title": "核心交换机故障", "priority": "P0", "approver": "E-2048"}},
                request_id=5,
            )
            bob_try = await client.post(
                url, json=p0_body, headers=req_headers("tools/call", "create_ticket", write_token)
            )
            print(f"  bob（有 write、无 approve）建 P0 → status={bob_try.status_code}")
            print(f"    {json.dumps(bob_try.json()['error'], ensure_ascii=False)}")
            print(f"    WWW-Authenticate: {bob_try.headers.get('www-authenticate')}")

            carol_try = await client.post(
                url, json=p0_body, headers=req_headers("tools/call", "create_ticket", admin_token)
            )
            print(f"  carol（有 approve）建 P0 → status={carol_try.status_code}")
            print(f"    resultType={carol_try.json()['result']['resultType']}")
            print(f"    isError={carol_try.json()['result']['isError']}")
            print("  >>> 同一个工具、同一份参数结构，权限要求随 priority 变化。")
            print("      这就是'缺授权'——只能换有权限的人，不能靠补充信息解决。")

            header("第 8 步：过期 token → 401 expired_token")
            short = tokens.issue("dave", {SCOPE_READ}, ttl_s=-1)
            expired = await client.post(
                url, json=build_request("tools/list", request_id=6),
                headers=req_headers("tools/list", token=short),
            )
            print(f"  status={expired.status_code}  error={expired.json()['error']['code']}")
            print(f"  WWW-Authenticate: {expired.headers.get('www-authenticate')}")

    finally:
        server.shutdown()

    header("对比：两种'做不了'")
    print("  ┌────────────┬──────────────────────────┬─────────────────────────────┐")
    print("  │            │ 缺参数                   │ 缺权限                      │")
    print("  ├────────────┼──────────────────────────┼─────────────────────────────┤")
    print("  │ HTTP 状态  │ 200                      │ 403                         │")
    print("  │ 协议字段   │ resultType=input_required│ error=insufficient_scope    │")
    print("  │ 谁来补     │ 用户（回答一个问题）     │ 管理员（授予 scope）        │")
    print("  │ 重试方式   │ 原请求 + inputResponses  │ 换 token 后重发             │")
    print("  │ 问用户有用吗│ 有用                    │ 没用，问一百遍也没权限      │")
    print("  └────────────┴──────────────────────────┴─────────────────────────────┘")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
