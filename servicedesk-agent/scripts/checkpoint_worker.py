# file: scripts/checkpoint_worker.py
"""3.4 的子进程：跑到第 3 步时被"杀"（os._exit），用来测真实断点续跑。

用法（一般不直接调，由 checkpoint_demo 拉起）：
    CKPT_CRASH_AT=3 uv run python -m scripts.checkpoint_worker <db> <thread_id>
"""

from __future__ import annotations

import asyncio
import sys

from servicedesk.graph.checkpoint import build_workflow_graph, open_sqlite_checkpointer, run


async def main() -> int:
    if len(sys.argv) < 3:
        print("用法: python -m scripts.checkpoint_worker <db_path> <thread_id>", file=sys.stderr)
        return 2

    db_path, thread_id = sys.argv[1], sys.argv[2]

    async with open_sqlite_checkpointer(db_path) as saver:
        graph = build_workflow_graph(saver)
        out = await run(graph, thread_id, {"ticket_id": "T-1001", "trace": []})
        print(f"子进程正常完成: step={out.get('step')} trace={out.get('trace')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
