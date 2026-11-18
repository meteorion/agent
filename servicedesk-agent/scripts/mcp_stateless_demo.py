# file: scripts/mcp_stateless_demo.py
"""2.3 的场景：握手没了、session 没了，状态去哪了。

用法：
    uv run python -m scripts.mcp_stateless_demo
"""

from __future__ import annotations

import json

from servicedesk.mcp.envelope import (
    LEGACY_PROTOCOL_VERSION,
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


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def show_timelines() -> None:
    header("第 1 步：两张时序图")
    print("【旧版 2025-11-25：先握手，再干活】")
    print("""
  Client                                    Server
    │                                          │
    │ ① initialize (protocolVersion, caps)     │
    │ ────────────────────────────────────────►│
    │ ② InitializeResult                       │
    │ ◄────────────────────────────────────────│  ← 此处建立 session
    │    (Mcp-Session-Id: 7f3a…)               │
    │ ③ notifications/initialized              │
    │ ────────────────────────────────────────►│
    │ ④ tools/list                             │
    │ ──── Mcp-Session-Id: 7f3a… ─────────────►│
    │ ⑤ tools/call                             │
    │ ──── Mcp-Session-Id: 7f3a… ─────────────►│
    │                                          │
    ╰─ 问题：④⑤ 必须打到**同一台实例**，否则会话不存在
        → 要粘性路由，或把所有 session 塞进共享存储
""")

    print("【新版 2026-07-28：每个请求自带全部上下文】")
    print("""
  Client                                    Server（可以是任意一台实例）
    │                                          │
    │ ① server/discover（可选，想提前知道能力才调）│
    │ ────────────────────────────────────────►│
    │ ② DiscoverResult(versions, caps, identity)│
    │ ◄────────────────────────────────────────│
    │ ③ tools/call                             │
    │    _meta: {protocolVersion, clientInfo,  │
    │            clientCapabilities}           │
    │    headers: MCP-Protocol-Version,        │
    │             Mcp-Method, Mcp-Name         │
    │ ────────────────────────────────────────►│
    │ ④ resultType: "complete"                 │
    │ ◄────────────────────────────────────────│
    │                                          │
    ╰─ 没有 session，所以 ③④ 可以落在**任何一台**实例上
        → 普通轮询负载均衡即可，无需共享存储
""")


def show_request() -> None:
    header("第 2 步：一个自描述的请求长什么样")
    request = build_request(
        "tools/call",
        {"name": "query_asset", "arguments": {"hostname": "NB-1024"}},
        client_name="servicedesk-agent",
        client_version="0.1.0",
        client_capabilities={"elicitation": {}},
        log_level="info",
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        request_id=1,
    )
    headers_demo = build_headers(
        "tools/call", name="query_asset", traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    )
    print("  请求体：")
    print("    " + json.dumps(request, ensure_ascii=False, indent=2).replace("\n", "\n    "))
    print("\n  HTTP 头：")
    for key, value in headers_demo.items():
        print(f"    {key}: {value}")
    print("\n  >>> 注意三点：")
    print("      1. 协议版本在 _meta 里（HTTP 上同时放进 MCP-Protocol-Version 头）")
    print("      2. 日志级别用 _meta.logLevel 逐请求声明（旧版的 logging/setLevel 已移除）")
    print("      3. traceparent 也走 _meta，链路能跨进程连起来")


def show_version_negotiation() -> None:
    header("第 3 步：版本不匹配会怎样")
    server_supported = [PROTOCOL_VERSION]

    print(f"  服务端支持：{server_supported}")
    print()

    for requested in (PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION):
        print(f"  客户端声明 {requested!r}：", end="")
        try:
            accepted = check_protocol_version(requested, server_supported)
            print(f"✅ 接受（{accepted}）")
        except UnsupportedProtocolVersionError as exc:
            print("❌ 拒绝")
            print(f"       错误信息：{exc}")
            print(f"       客户端可据 supported={exc.supported} 重试或降级到 legacy 通道")

    print("\n  >>> 旧版是'握手时协商一次，之后都按这个版本'；")
    print("      新版是'每个请求都声明，服务端逐请求判断'——这样才能路由到任意实例。")

    header("第 4 步：server/discover 的容错读取")
    sample = {"protocolVersions": [PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION]}
    print(f"  模拟返回：{sample}")
    print(f"  读出支持的版本：{supported_versions(sample)}")
    print(f"  字段名不同也能读：{supported_versions({'supportedProtocolVersions': ['2026-07-28']})}")
    print(f"  读不出来就返回空表（不瞎猜）：{supported_versions({'foo': 1})}")
    print("  ⚠️ 返回体确切字段名以官方 schema 为准，这里只演示容错策略。")


def show_result_type_compat() -> None:
    header("第 5 步：resultType 的向后兼容")
    cases = {
        "新版普通结果": {"result": {"resultType": "complete", "content": []}},
        "新版要补充输入": {"result": {"resultType": "input_required", "inputRequests": []}},
        "旧版 Server（没有该字段）": {"result": {"content": []}},
        "奇怪的值": {"result": {"resultType": "weird"}},
        "错误响应（没有 result）": {"error": {"code": -32602, "message": "bad params"}},
    }
    for label, response in cases.items():
        print(f"  {label:<24} → resultType={result_type(response)!r}"
              f"  input_required={is_input_required(response)}")
    print("\n  >>> 铁律：**缺失或不认识一律按 complete 处理**。")
    print("      注意最后一行：错误响应压根没有 result，调用方要先看 error 字段，")
    print("      再谈 resultType——顺序反了会把 400 当成成功。")
    print("      否则所有 2025-11-25 及更早的服务端会在一夜之间全部失效。")


def show_mrtr() -> None:
    header("第 6 步：MRTR —— 服务端说「我还需要你补两个信息」")
    first = build_request(
        "tools/call",
        {"name": "create_ticket", "arguments": {"title": "VPN 连不上"}},
        request_id=7,
    )
    print("  第一轮请求：")
    print(f"    method={first['method']}  id={first['id']}  "
          f"name={first['params']['name']}")

    server_response = {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {
            "resultType": "input_required",
            "inputRequests": [
                {"id": "prio", "prompt": "这条工单的优先级是？", "schema": {"enum": ["P0", "P1", "P2", "P3"]}},
                {"id": "approve", "prompt": "P1 需要主管审批，是否已审批？", "schema": {"type": "boolean"}},
            ],
        },
    }
    print("\n  服务端返回 resultType=input_required，需要补充：")
    for item in input_requests(server_response):
        print(f"    - [{item['id']}] {item['prompt']}")

    retried = build_retry_with_inputs(first, {"prio": "P1", "approve": True})
    print("\n  客户端**重试原请求**，把答案放进 inputResponses：")
    print("    " + json.dumps(retried["params"], ensure_ascii=False)[:220] + "…")
    print("\n  >>> 注意：方法、参数、id 全部不变，只多了一个 inputResponses。")
    print("      所以服务端**不需要记住**上一轮问过什么——状态天然不需要。")


def show_serverless() -> None:
    header("第 7 步：为什么这版可以 Serverless 冷启动")
    print("  模拟一个普通轮询负载均衡：3 个实例，互不通信，都不存会话。\n")

    store = HandleStore(ttl_s=60)

    print("  【做法 A：状态藏在实例内存里（旧版思路）】")
    memory: dict[str, dict[str, str]] = {"i-1": {}, "i-2": {}, "i-3": {}}
    memory["i-1"]["session"] = {"user": "张伟", "step": 1}
    print("    请求 1 → i-1：建立 session，记录 user=张伟")
    print(f"    请求 2 → i-2：查内存 → {memory['i-2'].get('session', '找不到 session')} ❌")

    print("\n  【做法 B：状态放在显式句柄里（新版思路）】")
    handle = store.mint({"user": "张伟", "step": 1})
    print(f"    请求 1 → i-1：工具返回 handle={handle}")
    for instance in ("i-2", "i-3"):
        state = store.resolve(handle)
        print(f"    请求 2 → {instance}：模型把 handle 当参数传回 → {state} ✅")

    print(f"\n  句柄存储里有 {len(store)} 条记录；实例本身**没有任何状态**。")
    print("  过期后再用同一个 handle：", store.resolve("h_expired") or "None（已失效，提示用户重来）")
    print("\n  >>> 官方 blog 的原话大意：让模型看见 handle 并在工具之间传递，")
    print("      比把状态藏在传输层更好——模型能理解、能重放、能调试。")


def main() -> int:
    show_timelines()
    show_request()
    show_version_negotiation()
    show_result_type_compat()
    show_mrtr()
    show_serverless()

    header("小结")
    print("  1. 没有握手：每个请求自带 protocolVersion / clientInfo / capabilities。")
    print("  2. 没有 session：跨调用状态改用「服务端签发的显式句柄」，由模型当参数带回来。")
    print("  3. 版本逐请求判断，不支持就抛 UnsupportedProtocolVersionError 并列出支持版本。")
    print("  4. resultType 缺失时按 complete 处理——这是旧服务端还能用的唯一原因。")
    print("  5. 没有传输层状态，才能让任意请求落到任意实例：这就是 Serverless 的前提。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
