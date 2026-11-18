# file: scripts/mcp_server_demo.py
"""2.4 的场景：一个无状态 MCP Server 的完整往返。

用法：
    uv run python -m scripts.mcp_server_demo

真实 stdio 验证（把请求喂给独立进程）：
    '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{}}' |
        uv run python -m servicedesk.mcp.server
"""

from __future__ import annotations

import asyncio
import io
import json
import sys

from servicedesk.logging_setup import configure_logging
from servicedesk.mcp.envelope import build_request
from servicedesk.mcp.server import McpServer, serve_stdio


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def show(label: str, response: dict) -> None:
    print(f"\n  【{label}】")
    print("    " + json.dumps(response, ensure_ascii=False)[:520])


async def main() -> int:
    # 工具层的 structlog 也必须写 stderr —— 否则一行日志就会污染协议通道
    configure_logging(level="INFO", stream=sys.stderr)
    server = McpServer(log_stream=io.StringIO())   # 日志单独收，别混进输出

    header("第 1 步：server/discover —— 服务器必须先说自己是谁")
    discover = await server.handle_async(
        build_request("server/discover", request_id=1)
    )
    show("discover", discover)
    result = discover["result"]
    print(f"\n    支持的版本: {result['protocolVersions']}")
    print(f"    身份      : {result['serverInfo']}")
    print(f"    能力      : {sorted(result['capabilities'])}")
    print(f"    resultType: {result['resultType']}")

    header("第 2 步：tools/list —— 确定性排序 + 缓存提示")
    tools1 = await server.handle_async(build_request("tools/list", request_id=2))
    tools2 = await server.handle_async(build_request("tools/list", request_id=3))
    names = [tool["name"] for tool in tools1["result"]["tools"]]
    print(f"    工具（已排序）: {names}")
    print(f"    ttlMs={tools1['result']['ttlMs']}  cacheScope={tools1['result']['cacheScope']}")
    same = json.dumps(tools1["result"], sort_keys=True) == json.dumps(tools2["result"], sort_keys=True)
    print(f"    两次调用结果完全一致: {same}   ← prompt cache 稳定的前提")

    header("第 3 步：tools/call —— 复用 2.1 的工具注册表")
    call_ok = await server.handle_async(
        build_request(
            "tools/call",
            {"name": "query_asset", "arguments": {"hostname": "NB-1024"}},
            request_id=4,
        )
    )
    show("调用成功", call_ok)

    call_bad = await server.handle_async(
        build_request(
            "tools/call",
            {"name": "query_asset", "arguments": {"hostname": "x"}},
            request_id=5,
        )
    )
    show("参数校验失败（isError=true，但协议层仍是成功响应）", call_bad)

    call_unknown = await server.handle_async(
        build_request("tools/call", {"name": "query_wifi", "arguments": {}}, request_id=6)
    )
    show("未知工具", call_unknown)

    header("第 4 步：Resources —— 只读数据，由 Host 决定挂载")
    resources = await server.handle_async(build_request("resources/list", request_id=7))
    show("resources/list", resources)
    read = await server.handle_async(
        build_request("resources/read", {"uri": "doc://vpn-guide"}, request_id=8)
    )
    contents = read["result"]["contents"][0]
    print(f"\n    读到 {len(contents['text'])} 字符，mimeType={contents['mimeType']}")
    print("    " + contents["text"].splitlines()[0])

    header("第 5 步：Prompts —— 给用户点的模板，不是给程序调的")
    prompts = await server.handle_async(build_request("prompts/list", request_id=9))
    show("prompts/list", prompts)
    got = await server.handle_async(
        build_request(
            "prompts/get",
            {"name": "explain_ticket", "arguments": {"ticket_id": "T-1001"}},
            request_id=10,
        )
    )
    show("prompts/get", got)

    header("第 6 步：错误路径")
    unknown = await server.handle_async(
        build_request("tools/nonexistent", request_id=11)
    )
    print(f"  未知方法      → code={unknown['error']['code']} message={unknown['error']['message']}")
    bad_uri = await server.handle_async(
        build_request("resources/read", {"uri": "doc://nope"}, request_id=12)
    )
    print(f"  未知资源      → code={bad_uri['error']['code']} message={bad_uri['error']['message']}")
    # 2.7 起 Server 是 dual-era：2025-11-25（legacy）会被接受
    legacy = await server.handle_async(
        build_request("tools/list", protocol_version="2025-11-25", request_id=12)
    )
    print(f"  旧版声明      → {'接受（dual-era）' if 'result' in legacy else '拒绝'}"
          f"  ← 2.7 为兼容未升级客户端而保留")
    bad_version = await server.handle_async(
        build_request("tools/list", protocol_version="1999-01-01", request_id=13)
    )
    print(f"  版本不支持    → code={bad_version['error']['code']} message={bad_version['error']['message']}")
    print(f"                    data={bad_version['error'].get('data')}")

    header("第 7 步：stdio 帧格式（一行一条 JSON-RPC）")
    stdin = io.StringIO(
        '{"jsonrpc":"2.0","id":"a","method":"server/discover","params":{}}\n'
        '{"jsonrpc":"2.0","id":"b","method":"tools/list","params":{}}\n'
        "这不是 JSON\n"
    )
    stdout = io.StringIO()
    await serve_stdio(McpServer(log_stream=io.StringIO()), stdin=stdin, stdout=stdout)
    for index, line in enumerate(stdout.getvalue().splitlines(), start=1):
        print(f"    第 {index} 行输出: {line[:100]}…")
    print("\n    >>> 注意第 3 行输入是坏的，服务端返回 -32700，**没有崩**。")
    print("    >>> 而且所有输出都在 stdout 里按行；日志走 stderr，两者不混。")

    header("小结")
    print("  1. server/discover 是服务器的'自我介绍'，必须实现。")
    print("  2. tools/list 要确定性排序 + ttlMs/cacheScope，否则客户端没法缓存。")
    print("  3. 工具失败通过 isError=true 表达，协议层仍然是成功响应——两层语义要分开。")
    print("  4. Resources 是数据、Prompts 是模板：前者 Host 挂载，后者用户选择。")
    print("  5. stdout 只能写协议；日志必须走 stderr。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
