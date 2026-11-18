# file: src/servicedesk/a2a/client.py
"""A2A 客户端：发现 → 派活 → 轮询 → 取结果。

四步对应服务端的四个能力：
    1. GET  /.well-known/agent-card.json   看对方能干什么
    2. POST /  message/send                派活（立刻拿到 task_id）
    3. POST /  tasks/get                   轮询（长任务必需）
    4. POST /  tasks/cancel                取消
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from servicedesk.a2a.server import CARD_PATH, TERMINAL_STATES


@dataclass
class AgentEndpoint:
    """发现阶段拿到的东西。"""

    base_url: str
    card: dict[str, Any]

    @property
    def skills(self) -> list[dict[str, Any]]:
        return self.card.get("skills") or []

    def skill_ids(self) -> list[str]:
        return [skill.get("id", "") for skill in self.skills]

    def find_skill(self, keyword: str) -> dict[str, Any] | None:
        for skill in self.skills:
            haystack = " ".join(
                [skill.get("id", ""), skill.get("name", ""), skill.get("description", "")]
                + list(skill.get("tags") or [])
            )
            if keyword in haystack:
                return skill
        return None


class A2AClient:
    def __init__(self, base_url: str, *, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    # -- 发现 --

    async def discover(self, client: httpx.AsyncClient | None = None) -> AgentEndpoint:
        own = client is None
        client = client or httpx.AsyncClient(timeout=self.timeout_s)
        try:
            response = await client.get(f"{self.base_url}{CARD_PATH}")
            response.raise_for_status()
            return AgentEndpoint(base_url=self.base_url, card=response.json())
        finally:
            if own:
                await client.aclose()

    # -- 派活 --

    async def send(
        self,
        text: str,
        *,
        client: httpx.AsyncClient,
        task_id: str | None = None,
        context_id: str | None = None,
        skill_id: str | None = None,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "messageId": f"msg_{uuid.uuid4().hex[:12]}",
            "role": "user",
            "parts": [{"kind": "text", "text": text}],
        }
        if task_id:
            message["taskId"] = task_id
        if context_id:
            message["contextId"] = context_id
        if skill_id:
            message["metadata"] = {"skillId": skill_id}

        payload = {
            "jsonrpc": "2.0",
            "id": f"rpc_{uuid.uuid4().hex[:8]}",
            "method": "message/send",
            "params": {"message": message},
        }
        response = await client.post(f"{self.base_url}/", json=payload)
        body = response.json()
        if "error" in body:
            raise RuntimeError(f"A2A 错误：{body['error']}")
        return body["result"]

    async def get(self, task_id: str, *, client: httpx.AsyncClient) -> dict[str, Any]:
        response = await client.post(
            f"{self.base_url}/",
            json={"jsonrpc": "2.0", "id": "rpc_get", "method": "tasks/get", "params": {"id": task_id}},
        )
        body = response.json()
        if "error" in body:
            raise RuntimeError(f"A2A 错误：{body['error']}")
        return body["result"]

    async def cancel(self, task_id: str, *, client: httpx.AsyncClient) -> dict[str, Any]:
        response = await client.post(
            f"{self.base_url}/",
            json={"jsonrpc": "2.0", "id": "rpc_cancel", "method": "tasks/cancel", "params": {"id": task_id}},
        )
        body = response.json()
        if "error" in body:
            raise RuntimeError(f"A2A 错误：{body['error']}")
        return body["result"]

    async def wait(
        self,
        task_id: str,
        *,
        client: httpx.AsyncClient,
        timeout_s: float = 20.0,
        interval_s: float = 0.1,
        on_state: Any = None,
        stop_on_input_required: bool = True,
        known_state: str | None = None,
    ) -> dict[str, Any]:
        """轮询到终态；默认遇到 input-required 也返回（等客户端补信息）。

        补完信息后再调 `wait` 时要传 `stop_on_input_required=False`，
        否则会立刻在"还没被后台线程推进"的旧状态上返回。
        """
        deadline = time.monotonic() + timeout_s
        # 已知状态不重复上报：补信息后第一次轮询往往还是旧状态
        last_state = known_state or ""
        while time.monotonic() < deadline:
            task = await self.get(task_id, client=client)
            state = task["status"]["state"]
            if state != last_state:
                last_state = state
                if on_state:
                    on_state(task)
            if state in TERMINAL_STATES or (stop_on_input_required and state == "input-required"):
                return task
            await asyncio.sleep(interval_s)
        raise TimeoutError(f"等待任务 {task_id} 超时（最后状态 {last_state}）")


def artifact_text(task: dict[str, Any]) -> str:
    """把任务产出的 artifacts 拼成文本，方便展示。"""
    lines: list[str] = []
    for artifact in task.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if part.get("kind") == "text":
                lines.append(part["text"])
    return "\n".join(lines)
