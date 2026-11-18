# file: src/servicedesk/a2a/server.py
"""A2A Server：让别的 Agent 能"发现我、把活派给我"。

四个要素（对应 A2A v1.0）：

    Agent Card   GET /.well-known/agent-card.json   —— 能力发现（公开元数据）
    message/send POST /                             —— 提交任务（旧名 tasks/send 兼容）
    tasks/get    POST /                             —— 轮询状态（长任务必需）
    tasks/cancel POST /                             —— 取消

任务状态机（本节实现其中 6 个）：

    submitted → working → completed
                  ├── input-required →（补信息）→ working
                  ├── canceled
                  └── failed

⚠️ 待核实：`configuration`（如 `blocking`/`returnImmediately`）与 `artifact` 的完整字段
在 v1.0 里仍有演进，本节只实现任务流所需的最小字段集。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CARD_PATH = "/.well-known/agent-card.json"
RPC_PATH = "/"

# 任务状态（终态：completed / canceled / failed / rejected）
STATE_SUBMITTED = "submitted"
STATE_WORKING = "working"
STATE_INPUT_REQUIRED = "input-required"
STATE_COMPLETED = "completed"
STATE_CANCELED = "canceled"
STATE_FAILED = "failed"

TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_CANCELED, STATE_FAILED})


# ------------------------------- Agent Card -------------------------------


def build_agent_card(*, base_url: str, name: str = "servicedesk-agent") -> dict[str, Any]:
    """能力卡片：对方拿到它就知道"你能干什么、怎么调你"。"""
    return {
        "name": name,
        "description": "IT 服务台 Agent：查设备、查权限、检索知识库、建工单。",
        "version": "0.1.0",
        "url": base_url,
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "it_support",
                "name": "IT 支持",
                "description": "根据症状描述给出排查步骤，必要时要求补充设备信息。",
                "tags": ["it", "vpn", "email"],
                "examples": ["NB-1024 的 VPN 连不上怎么办"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            }
        ],
    }


# ------------------------------- 任务模型 -------------------------------


@dataclass
class Task:
    id: str
    context_id: str
    state: str
    created_at: float
    updated_at: float
    history: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    status_text: str = ""
    # 内部用：待补充的信息
    missing: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": {
                "state": self.state,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.updated_at)),
                "message": {"role": "agent", "parts": [{"kind": "text", "text": self.status_text}]}
                if self.status_text
                else None,
            },
            "history": self.history,
            "artifacts": self.artifacts,
        }


def _text_of(message: dict[str, Any]) -> str:
    return " ".join(
        part.get("text", "") for part in message.get("parts") or [] if part.get("kind") == "text"
    ).strip()


class TaskStore:
    """内存任务表。生产里换成 Redis/DB，否则进程一停"活"就没了（3.4 的教训）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def create(self, context_id: str, text: str) -> Task:
        now = time.time()
        task = Task(
            id=f"task_{uuid.uuid4().hex[:12]}",
            context_id=context_id,
            state=STATE_SUBMITTED,
            created_at=now,
            updated_at=now,
            history=[{"role": "user", "parts": [{"kind": "text", "text": text}]}],
            status_text="任务已接收",
        )
        with self._lock:
            self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def all(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())


# ------------------------------- 业务逻辑 -------------------------------

def _mentions_device(text: str) -> bool:
    """用户有没有给出设备信息（NB-xxxx 或明确说了 hostname）。"""
    return "nb-" in text.lower() or "hostname" in text.lower()


def _history_texts(task: Task) -> list[str]:
    texts: list[str] = []
    for message in task.history:
        for part in message.get("parts") or []:
            if part.get("kind") == "text":
                texts.append(part.get("text", ""))
    return texts


def _run_task(store: TaskStore, task: Task, text: str) -> None:
    """后台推进任务状态（模拟一个"要跑几秒"的活）。"""
    time.sleep(0.15)
    if task.state == STATE_CANCELED:
        return

    # 第一轮：缺设备号就问（真实系统里这就是 input-required 的用法）
    if not any(_mentions_device(item) for item in _history_texts(task)):
        with store._lock:  # noqa: SLF001
            task.state = STATE_INPUT_REQUIRED
            task.updated_at = time.time()
            task.missing = "device_hostname"
            task.status_text = "请提供设备主机名（形如 NB-1024），我才能查资产与权限。"
        return

    with store._lock:  # noqa: SLF001
        task.state = STATE_WORKING
        task.updated_at = time.time()
        task.status_text = "正在检索知识库与设备信息…"

    time.sleep(0.25)
    if task.state == STATE_CANCELED:
        return

    reply = (
        "排查建议：1) 在自助门户重置密码；2) 校准手机时间；"
        "3) 仍失败则提交工单。认证失败常见于密码过期与动态口令偏差。"
    )
    with store._lock:  # noqa: SLF001
        task.state = STATE_COMPLETED
        task.updated_at = time.time()
        task.status_text = "已完成"
        task.artifacts.append(
            {
                "artifactId": f"art_{uuid.uuid4().hex[:8]}",
                "name": "排查结论",
                "parts": [{"kind": "text", "text": reply}],
            }
        )
        task.history.append({"role": "agent", "parts": [{"kind": "text", "text": reply}]})


# ------------------------------- JSON-RPC -------------------------------


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_rpc(store: TaskStore, payload: dict[str, Any]) -> dict[str, Any]:
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    # message/send 是 v1.0 的名字；tasks/send 是被取代的旧名，保留兼容
    if method in ("message/send", "tasks/send"):
        message = params.get("message") or params
        text = _text_of(message)
        if not text:
            return _error(request_id, -32602, "message.parts 里没有文本")

        task_id = message.get("taskId")
        if task_id:
            task = store.get(task_id)
            if task is None:
                return _error(request_id, -32602, f"任务不存在：{task_id}")
            if task.state in TERMINAL_STATES:
                return _error(request_id, -32600, "终态任务不能继续")
            task.history.append({"role": "user", "parts": [{"kind": "text", "text": text}]})
            task.missing = None
        else:
            task = store.create(message.get("contextId") or f"ctx_{uuid.uuid4().hex[:8]}", text)

        threading.Thread(target=_run_task, args=(store, task, text), daemon=True).start()
        return _result(request_id, task.as_dict())

    if method == "tasks/get":
        task = store.get(str(params.get("id") or ""))
        if task is None:
            return _error(request_id, -32602, "任务不存在")
        return _result(request_id, task.as_dict())

    if method == "tasks/cancel":
        task = store.get(str(params.get("id") or ""))
        if task is None:
            return _error(request_id, -32602, "任务不存在")
        if task.state in TERMINAL_STATES:
            return _error(request_id, -32600, f"任务已是终态：{task.state}")
        with store._lock:  # noqa: SLF001
            task.state = STATE_CANCELED
            task.updated_at = time.time()
            task.status_text = "已取消"
        return _result(request_id, task.as_dict())

    if method == "tasks/list":
        return _result(request_id, {"tasks": [task.as_dict() for task in store.all()]})

    return _error(request_id, -32601, f"未知方法：{method}")


# ------------------------------- HTTP -------------------------------


class A2AHandler(BaseHTTPRequestHandler):
    server_version = "A2AServer/0.1"
    store: TaskStore
    base_url: str

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == CARD_PATH:
            self._json(200, build_agent_card(base_url=self.base_url))
            return
        self._json(404, {"error": {"message": f"未知路径 {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._json(400, _error(None, -32700, f"JSON 解析失败：{exc.msg}"))
            return

        try:
            response = handle_rpc(self.store, payload)
        except Exception as exc:  # noqa: BLE001
            response = _error(payload.get("id"), -32603, f"内部错误：{exc}")
        self._json(200, response)

    def _json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except ConnectionError:
            return


def create_server(*, host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """起一个 A2A Server。port=0 → 随机端口。返回 (server, base_url)。"""
    store = TaskStore()

    class Bound(A2AHandler):
        pass

    server = ThreadingHTTPServer((host, port), Bound)
    real_port = server.server_address[1]
    base_url = f"http://{host}:{real_port}"
    Bound.store = store
    Bound.base_url = base_url

    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, base_url


def main() -> int:
    import os
    import sys

    port = int(os.environ.get("PORT", "8080"))
    server, base_url = create_server(port=port)
    print(f"A2A Server 已启动: {base_url}", flush=True)
    print(f"Agent Card: {base_url}{CARD_PATH}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
