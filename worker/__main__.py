"""RQ worker process (SimpleWorker — no fork, Windows + Playwright friendly)."""

from __future__ import annotations

import sys

from rq import SimpleWorker

from settings import get_settings
from worker.queue import get_queue, get_redis, ping_redis


def main() -> int:
    settings = get_settings()
    if not ping_redis():
        print(f"error: cannot ping Redis at {settings.redis_url}", file=sys.stderr)
        return 1
    queue = get_queue()
    print(f"[worker] queue={settings.rq_queue} redis={settings.redis_url}")
    print("[worker] using SimpleWorker (no fork; safe for Windows / Playwright)")
    worker = SimpleWorker([queue], connection=get_redis())
    worker.work()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
