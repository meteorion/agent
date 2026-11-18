# file: scripts/mcp_http_demo.py
"""2.5 的场景：Streamable HTTP、订阅流、两个实例并发、跨实例句柄。

用法：
    uv run python -m scripts.mcp_http_demo
"""

from __future__ import annotations

import asyncio
import json

import httpx

from servicedesk.mcp.envelope import PROTOCOL_VERSION, HandleStore, build_request
from servicedesk.mcp.http_app import create_server
from servicedesk.logging_setup import configure_logging


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def req_headers(method: str, name: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name:
        headers["Mcp-Name"] = name
    return headers


async def main() -> int:
    configure_logging(level="ERROR")

    # 客户端主动断开订阅流时，Windows 的 Proactor 会刷 ConnectionResetError
    loop = asyncio.get_running_loop()

    def _quiet_reset(loop_: asyncio.AbstractEventLoop, context: dict) -> None:
        if isinstance(context.get("exception"), ConnectionResetError):
            return
        loop_.default_exception_handler(context)

    loop.set_exception_handler(_quiet_reset)

    server_a, url_a = create_server()
    server_b, url_b = create_server()
    print(f"实例 A: {url_a}")
    print(f"实例 B: {url_b}")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            header("第 1 步：单端点 POST + 路由头")
            discover = await client.post(
                url_a,
                json=build_request("server/discover", request_id=1),
                headers=req_headers("server/discover"),
            )
            result = discover.json()["result"]
            print(f"  status={discover.status_code}  resultType={result['resultType']}")
            print(f"  versions={result['protocolVersions']}  server={result['serverInfo']['name']}")

            header("第 2 步：路由头是硬要求")
            no_header = await client.post(
                url_a, json=build_request("tools/list", request_id=2), headers={"Content-Type": "application/json"}
            )
            print(f"  缺 Mcp-Method     → {no_header.status_code}  {no_header.json()['error']['message']}")

            spoof = await client.post(
                url_a,
                json=build_request("tools/call", {"name": "query_asset", "arguments": {"hostname": "NB-1024"}}, request_id=3),
                headers=req_headers("tools/list"),      # 头写 list，体里是 call
            )
            print(f"  头与体不一致      → {spoof.status_code}  {spoof.json()['error']['message']}")

            bad_version = await client.post(
                url_a,
                json=build_request("tools/list", request_id=4),
                headers={**req_headers("tools/list"), "MCP-Protocol-Version": "1999-01-01"},
            )
            print(f"  版本头不支持      → {bad_version.status_code}  "
                  f"{bad_version.json()['error']['message']}")

            header("第 3 步：tools/call 走 HTTP")
            call = await client.post(
                url_a,
                json=build_request(
                    "tools/call",
                    {"name": "query_permission", "arguments": {"employee_id": "E-2048"}},
                    request_id=5,
                ),
                headers=req_headers("tools/call", "query_permission"),
            )
            payload = call.json()["result"]
            print(f"  isError={payload['isError']}")
            print(f"  内容: {payload['content'][0]['text'][:120]}")

            header("第 4 步：subscriptions/listen —— 一条长连接 POST 响应流")
            events: list[dict] = []
            listen_body = build_request(
                "subscriptions/listen",
                {"types": ["toolsListChanged", "promptsListChanged", "bogusType"]},
                request_id=6,
            )
            with httpx.stream(
                "POST", url_a, json=listen_body, headers=req_headers("subscriptions/listen"),
                timeout=10.0,
            ) as response:
                print(f"  status={response.status_code}  "
                      f"content-type={response.headers.get('content-type')}")
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    events.append(json.loads(line[5:].strip()))
                    if len(events) >= 4:          # 提前断开，模拟客户端中途离开
                        break

            ack = events[0]["result"]
            print(f"  订阅 id   : {ack['subscriptionId']}")
            print(f"  已接受类型: {ack['types']}")
            print(f"  已拒绝类型: {ack['rejected']}   ← 未知类型不会让整条流失败")
            for event in events[1:]:
                meta = event["params"]["_meta"]
                print(f"  通知 {event['method']:<34} seq={event['params']['seq']} "
                      f"subscriptionId={meta['io.modelcontextprotocol/subscriptionId'][:12]}…")

            header("第 5 步：流断了 = 请求丢了，必须用新 id 重发")
            print(f"  第一次订阅用的 request id: {listen_body['id']}")
            resend = build_request(
                "subscriptions/listen", {"types": ["toolsListChanged"]}, request_id=7
            )
            with httpx.stream(
                "POST", url_a, json=resend, headers=req_headers("subscriptions/listen"),
                timeout=10.0,
            ) as response:
                first = next(
                    json.loads(line[5:].strip())
                    for line in response.iter_lines()
                    if line.startswith("data:")
                )
            print(f"  重发用的 request id      : {resend['id']}   ← 必须是新的")
            print(f"  新 subscriptionId        : {first['result']['subscriptionId'][:12]}…"
                  f"（与上次不同：{first['result']['subscriptionId'] != ack['subscriptionId']}）")
            print("  >>> 没有 Last-Event-ID，所以不能'续上'，只能重新订阅。")

            header("第 6 步：两个实例并发，互不影响")
            tasks = [
                client.post(
                    url,
                    json=build_request("tools/call",
                                       {"name": "query_asset", "arguments": {"hostname": host}},
                                       request_id=100 + index),
                    headers=req_headers("tools/call", "query_asset"),
                )
                for index, (url, host) in enumerate(
                    [(url_a, "NB-1024"), (url_b, "NB-2077")]
                )
            ]
            responses = await asyncio.gather(*tasks)
            for label, response in zip(("A", "B"), responses):
                body = response.json()["result"]
                text = json.loads(body["content"][0]["text"])
                print(f"  实例 {label}: isError={body['isError']}  owner={text['owner']}")
            print("  >>> 两个请求落在两台互不通信的实例上，都成功。")

            header("第 7 步：跨实例的「状态」——放在句柄里，不放在实例里")
            shared = HandleStore()
            handle = shared.mint({"user": "张伟", "step": 2})
            print(f"  实例 A 的工具签发了 handle={handle}")
            for label, url in (("A", url_a), ("B", url_b)):
                resolved = shared.resolve(handle)
                print(f"  实例 {label} 用这个 handle 取状态 → {resolved}")
            print("  >>> 实例本身没有任何状态；谁能读到 store，谁就能接着处理。")
            print("      生产里这个 store 是 Redis/DB，且必须做归属校验（2.3 §6.2）。")

    finally:
        server_a.shutdown()
        server_b.shutdown()

    header("小结")
    print("  1. 单端点 POST + Mcp-Method/Mcp-Name 头：网关不解析 body 就能路由。")
    print("  2. subscriptions/listen 是一条长连接 POST 响应流，按类型 opt-in。")
    print("  3. 流断了就是丢了：没有续传，客户端必须用**新 request id** 重发。")
    print("  4. 实例不存状态，所以并发打到不同实例都成功，Serverless 才成立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
