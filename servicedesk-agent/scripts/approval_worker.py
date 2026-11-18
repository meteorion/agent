# file: scripts/approval_worker.py
"""3.5 的子进程：发起审批后立刻退出（模拟"挂起期间进程没了"）。

用法：
    uv run python -m scripts.approval_worker <db> <thread_id> <ticket_id>
"""

from __future__ import annotations

import asyncio
import sys

from servicedesk.graph.approval import build_approval_graph, open_approval_checkpointer, start


async def main() -> int:
    if len(sys.argv) < 4:
        print("用法: python -m scripts.approval_worker <db> <thread_id> <ticket_id>", file=sys.stderr)
        return 2

    db_path, thread_id, ticket_id = sys.argv[1], sys.argv[2], sys.argv[3]

    async with open_approval_checkpointer(db_path) as saver:
        graph = build_approval_graph(saver)
        out = await start(graph, thread_id, ticket_id=ticket_id, draft="重置密码的答复", risk="high")
        pending = out["pending"]
        print(f"子进程挂起: next={pending['next']} expires_at={pending['payload']['expires_at']:.0f}")

    # 子进程到此结束——图处于"挂起"状态，完全靠 SQLite 活着
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
