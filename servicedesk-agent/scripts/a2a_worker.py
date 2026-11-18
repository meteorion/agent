# file: scripts/a2a_worker.py
"""4.2 的子进程：一个独立的 A2A Server（真·另一个进程）。

用法：
    uv run python -m scripts.a2a_worker <port>
"""

from __future__ import annotations

import sys
import time

from servicedesk.a2a.server import CARD_PATH, create_server


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python -m scripts.a2a_worker <port>", file=sys.stderr)
        return 2

    port = int(sys.argv[1])
    server, base_url = create_server(port=port)
    print(f"READY {base_url}", flush=True)
    print(f"CARD {base_url}{CARD_PATH}", flush=True)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
