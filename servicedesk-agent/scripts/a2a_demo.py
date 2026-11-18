# file: scripts/a2a_demo.py
"""4.2 的场景：两个独立进程通过 A2A 协作。

用法：
    uv run python -m scripts.a2a_demo
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time

import httpx

from servicedesk.a2a.client import A2AClient, AgentEndpoint, artifact_text
from servicedesk.a2a.server import CARD_PATH
from servicedesk.logging_setup import configure_logging


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_ready(base_url: str, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(f"{base_url}{CARD_PATH}")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError(f"A2A Server 未在 {timeout_s}s 内就绪：{base_url}")


async def demo_discover(client: A2AClient) -> AgentEndpoint:
    header("第 1 步：能力发现 —— GET /.well-known/agent-card.json")
    endpoint = await client.discover()

    print(f"  name        : {endpoint.card['name']}")
    print(f"  description : {endpoint.card['description']}")
    print(f"  version     : {endpoint.card['version']}")
    print(f"  url         : {endpoint.card['url']}")
    print(f"  capabilities: {endpoint.card['capabilities']}")
    print()
    print("  skills:")
    for skill in endpoint.skills:
        print(f"    - {skill['id']:<12} {skill['name']}  tags={skill.get('tags')}")
        print(f"      {skill['description']}")

    matched = endpoint.find_skill("vpn")
    print()
    print(f"  按关键词 'vpn' 匹配到的 skill: {matched['id'] if matched else None}")
    print("  >>> 发现阶段**不产生任何任务**：先看清对方能干什么，再决定派不派活。")
    return endpoint


async def demo_task(client: A2AClient, http: httpx.AsyncClient) -> None:
    header("第 2 步：派活 —— message/send 立刻返回 task_id")

    started = time.monotonic()
    task = await client.send("VPN 连不上怎么办", client=http)
    elapsed = (time.monotonic() - started) * 1000

    print(f"  task_id   : {task['id']}")
    print(f"  contextId : {task['contextId']}")
    print(f"  state     : {task['status']['state']}")
    print(f"  返回耗时  : {elapsed:.0f}ms")
    print()
    print("  >>> 这就是长任务的正确打开方式：**先接单，再干活**。")
    print("      如果同步等结果，客户端就会卡在这里（3.6 讲的感知延迟）。")

    header("第 3 步：轮询 —— input-required 要补信息")
    timeline: list[tuple[float, str]] = []

    def on_state(current: dict) -> None:
        timeline.append((time.monotonic() - started, current["status"]["state"]))
        text = ((current["status"].get("message") or {}).get("parts") or [{}])[0].get("text", "")
        print(f"  [{timeline[-1][0]:5.2f}s] {current['status']['state']:<15} {text[:44]}")

    current = await client.wait(task["id"], client=http, on_state=on_state)
    if current["status"]["state"] == "input-required":
        print()
        print("  补上设备号，继续同一个任务（带 taskId，不是新建）：")
        await client.send("是 NB-1024 这台", client=http, task_id=task["id"])
        current = await client.wait(
            task["id"], client=http, on_state=on_state,
            stop_on_input_required=False, known_state="input-required",
        )

    print()
    print(f"  终态: {current['status']['state']}")
    print(f"  产出: {artifact_text(current)[:70]}…")
    print(f"  历史消息数: {len(current['history'])}")
    print()
    print("  >>> 注意补信息时**带上了 taskId**：服务端据此把这条消息并进同一个任务。")
    print("      不带 taskId 就会新建一个任务——原来那个永远停在 input-required。")


async def demo_cancel(client: A2AClient, http: httpx.AsyncClient) -> None:
    header("第 4 步：取消 —— 长任务必须能停")
    task = await client.send("NB-1024 的邮箱有问题", client=http)
    print(f"  新建任务: {task['id']}  state={task['status']['state']}")

    await asyncio.sleep(0.2)
    canceled = await client.cancel(task["id"], client=http)
    print(f"  取消后  : state={canceled['status']['state']}")

    try:
        await client.cancel(task["id"], client=http)
    except RuntimeError as exc:
        print(f"  再取消一次: {exc}")

    header("第 5 步：任务清单 —— tasks/list")
    response = await http.post(
        f"{client.base_url}/",
        json={"jsonrpc": "2.0", "id": "list", "method": "tasks/list", "params": {}},
    )
    tasks = response.json()["result"]["tasks"]
    for item in tasks:
        print(f"  {item['id']:<20} {item['status']['state']:<15} artifacts={len(item['artifacts'])}")


def compare() -> None:
    header("第 6 步：A2A 与 MCP 的边界")
    rows = [
        ("解决什么", "Agent ↔ Agent", "Agent ↔ 工具/数据"),
        ("对端是谁", "另一个会思考的 Agent", "一个可调用的能力"),
        ("交互形态", "派任务（长、异步、可取消）", "调函数（短、同步、幂等）"),
        ("状态", "任务有状态机（working/input-required…）", "单次请求-响应，无任务概念"),
        ("发现", "Agent Card（能力、技能、示例）", "tools/list（名字、schema）"),
        ("本节/前文", "4.2（本节）", "2.1–2.7"),
    ]
    print(f"  {'维度':<10}{'A2A':<34}{'MCP'}")
    print("  " + "-" * 78)
    for name, a2a, mcp in rows:
        print(f"  {name:<10}{a2a:<34}{mcp}")
    print()
    print("  >>> 记忆方法：**MCP 让 Agent 用手；A2A 让 Agent 找人。**")
    print("      一个 Agent 完全可以同时是 MCP 客户端和 A2A 服务端（本节就是）。")


async def main() -> int:
    configure_logging(level="ERROR")
    port = free_port()

    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.a2a_worker", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        ready_line = proc.stdout.readline().strip() if proc.stdout else ""
        if not ready_line.startswith("READY"):
            print(f"子进程启动失败: {ready_line}")
            return 1
        base_url = ready_line.split(" ", 1)[1]
        print(f"A2A Server（独立进程 pid={proc.pid}）: {base_url}")

        await wait_ready(base_url)
        client = A2AClient(base_url)

        async with httpx.AsyncClient(timeout=30.0) as http:
            await demo_discover(client)
            await demo_task(client, http)
            await demo_cancel(client, http)

        compare()

        header("小结")
        print("  1. Agent Card 让「派活之前先看清对方」成为可能。")
        print("  2. message/send 立刻返回 task_id，长任务靠轮询 / 流式推进。")
        print("  3. input-required 是状态机的一部分：补信息要带 taskId，否则会新建任务。")
        print("  4. A2A 管 Agent 之间，MCP 管 Agent 与工具之间——两者可以同时用。")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
