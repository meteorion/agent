# file: tests/test_a2a.py
"""4.2 的断言：Agent Card、任务状态机、取消、终态保护。"""

from __future__ import annotations

import pytest
import httpx

from servicedesk.a2a.client import A2AClient, artifact_text
from servicedesk.a2a.server import (
    CARD_PATH,
    STATE_CANCELED,
    STATE_COMPLETED,
    STATE_INPUT_REQUIRED,
    STATE_SUBMITTED,
    create_server,
)


@pytest.fixture()
def a2a():
    server, base_url = create_server()
    yield base_url
    server.shutdown()


@pytest.fixture()
def client(a2a: str) -> A2AClient:
    return A2AClient(a2a)


# ------------------------------ 发现 ------------------------------


async def test_agent_card_is_served(client: A2AClient) -> None:
    endpoint = await client.discover()
    card = endpoint.card

    assert card["name"] == "servicedesk-agent"
    assert card["url"].startswith("http://127.0.0.1:")
    assert card["capabilities"]["streaming"] is True
    assert endpoint.skill_ids() == ["it_support"]


async def test_find_skill_by_keyword(client: A2AClient) -> None:
    endpoint = await client.discover()
    assert endpoint.find_skill("vpn")["id"] == "it_support"
    assert endpoint.find_skill("根本没有这个词") is None


async def test_card_path_is_well_known(a2a: str) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.get(f"{a2a}{CARD_PATH}")
        missing = await http.get(f"{a2a}/nope")
    assert response.status_code == 200
    assert missing.status_code == 404


# ------------------------------ 派活 ------------------------------


async def test_send_returns_task_immediately(client: A2AClient) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        task = await client.send("VPN 连不上怎么办", client=http)

    assert task["id"].startswith("task_")
    assert task["contextId"]
    assert task["status"]["state"] in (STATE_SUBMITTED, STATE_INPUT_REQUIRED)


async def test_legacy_tasks_send_alias_still_works(a2a: str) -> None:
    """v1.0 用 message/send；旧名 tasks/send 保留兼容。"""
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.post(
            f"{a2a}/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tasks/send",
                "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "VPN 坏了"}]}},
            },
        )
    body = response.json()
    assert "result" in body
    assert body["result"]["id"].startswith("task_")


async def test_empty_message_is_rejected(a2a: str) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.post(
            f"{a2a}/",
            json={"jsonrpc": "2.0", "id": 1, "method": "message/send",
                  "params": {"message": {"role": "user", "parts": []}}},
        )
    assert response.json()["error"]["code"] == -32602


async def test_unknown_method_returns_method_not_found(a2a: str) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.post(
            f"{a2a}/", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/nuke", "params": {}}
        )
    assert response.json()["error"]["code"] == -32601


# ------------------------------ 状态机 ------------------------------


async def test_missing_device_leads_to_input_required(client: A2AClient) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        task = await client.send("VPN 连不上怎么办", client=http)
        current = await client.wait(task["id"], client=http)

    assert current["status"]["state"] == STATE_INPUT_REQUIRED
    text = current["status"]["message"]["parts"][0]["text"]
    assert "NB-" in text


async def test_follow_up_completes_the_same_task(client: A2AClient) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        task = await client.send("VPN 连不上怎么办", client=http)
        await client.wait(task["id"], client=http)

        await client.send("是 NB-1024 这台", client=http, task_id=task["id"])
        final = await client.wait(
            task["id"], client=http, stop_on_input_required=False, known_state=STATE_INPUT_REQUIRED
        )

    assert final["status"]["state"] == STATE_COMPLETED
    assert final["artifacts"]
    assert "重置密码" in artifact_text(final)
    assert len(final["history"]) == 3, "用户两条 + Agent 一条"


async def test_device_in_first_message_skips_input_required(client: A2AClient) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        task = await client.send("NB-1024 的 VPN 连不上", client=http)
        final = await client.wait(task["id"], client=http, stop_on_input_required=False)

    assert final["status"]["state"] == STATE_COMPLETED


async def test_terminal_task_cannot_be_continued(client: A2AClient) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        task = await client.send("NB-1024 的 VPN 连不上", client=http)
        await client.wait(task["id"], client=http, stop_on_input_required=False)

        with pytest.raises(RuntimeError, match="终态"):
            await client.send("再补一句", client=http, task_id=task["id"])


# ------------------------------ 取消与查询 ------------------------------


async def test_cancel_then_cancel_again_fails(client: A2AClient) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        task = await client.send("NB-1024 的邮箱有问题", client=http)
        canceled = await client.cancel(task["id"], client=http)

        assert canceled["status"]["state"] == STATE_CANCELED
        with pytest.raises(RuntimeError, match="终态"):
            await client.cancel(task["id"], client=http)


async def test_get_unknown_task_returns_error(a2a: str) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.post(
            f"{a2a}/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "task_nope"}},
        )
    assert response.json()["error"]["code"] == -32602


async def test_tasks_list_contains_created_tasks(client: A2AClient, a2a: str) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        await client.send("NB-1024 的 VPN 连不上", client=http)
        await client.send("NB-2077 的邮箱有问题", client=http)
        response = await http.post(
            f"{a2a}/", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/list", "params": {}}
        )

    tasks = response.json()["result"]["tasks"]
    assert len(tasks) == 2
    assert all(item["id"].startswith("task_") for item in tasks)
