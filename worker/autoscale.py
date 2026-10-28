"""Autoscaling worker supervisor.

Keeps a pool of ``python -m worker`` child processes sized to the RQ queue
depth: scale up when the queue grows, scale down idle workers when it drains.

- Each child is a normal RQ worker (SimpleWorker, no fork), identical to a
  manually started one, so nothing about task execution changes.
- Scaling down only targets children whose RQ state is ``idle`` (read from
  Redis ``rq:worker:{host}.{pid}``), which is safe on Windows (terminate is
  a hard kill there) and avoids interrupting running jobs everywhere.
- Run as ``python -m worker.autoscale`` (also the docker-compose worker CMD).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from rq import Queue

from settings import Settings, get_settings
from worker.queue import get_redis


@dataclass(frozen=True)
class ScalePolicy:
    min_workers: int = 1
    max_workers: int = 4
    jobs_per_worker: int = 1
    poll_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "ScalePolicy":
        return cls(
            min_workers=max(0, int(os.environ.get("WORKER_MIN", "1"))),
            max_workers=max(1, int(os.environ.get("WORKER_MAX", "4"))),
            jobs_per_worker=max(1, int(os.environ.get("SCALE_UP_THRESHOLD", "1"))),
            poll_seconds=max(1.0, float(os.environ.get("AUTOSCALE_POLL_SECONDS", "5"))),
        )

    def validate(self) -> None:
        if self.max_workers < self.min_workers:
            raise ValueError(
                f"WORKER_MAX ({self.max_workers}) must be >= WORKER_MIN ({self.min_workers})"
            )


def target_worker_count(depth: int, policy: ScalePolicy) -> int:
    """Desired pool size for a queue depth, bounded by min/max.

    - depth <= 0            -> min_workers
    - otherwise             -> min(max, ceil(depth / jobs_per_worker))
    """
    if depth <= 0:
        return policy.min_workers
    need = (depth + policy.jobs_per_worker - 1) // policy.jobs_per_worker
    return max(policy.min_workers, min(policy.max_workers, need))


class WorkerPool:
    """Manages child worker processes; polls queue depth and resizes."""

    def __init__(self, policy: ScalePolicy, settings: Settings | None = None) -> None:
        policy.validate()
        self.policy = policy
        self.settings = settings or get_settings()
        self.redis = get_redis()
        self._procs: dict[int, subprocess.Popen] = {}
        self._hostname = socket.gethostname()
        self._stop = False

    # ---- introspection ----

    @property
    def size(self) -> int:
        return len(self._procs)

    def queue_depth(self) -> int:
        try:
            return int(Queue(self.settings.rq_queue, connection=self.redis).count)
        except Exception:  # noqa: BLE001
            return 0

    def _worker_key(self, pid: int) -> str:
        # RQ default worker name is f"{hostname}.{pid}" for our SimpleWorker.
        return f"rq:worker:{self._hostname}.{pid}"

    def worker_state(self, pid: int) -> str | None:
        try:
            raw = self.redis.hget(self._worker_key(pid), "state")
        except Exception:  # noqa: BLE001
            return None
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return raw

    def _reap(self) -> None:
        for pid in list(self._procs):
            if self._procs[pid].poll() is not None:
                self._procs.pop(pid, None)

    # ---- scaling ----

    def _spawn(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-m", "worker"],
            env=os.environ.copy(),
        )
        self._procs[proc.pid] = proc
        print(f"[autoscale] spawned worker pid={proc.pid} (pool={self.size})", flush=True)

    def _terminate_idle(self, pid: int) -> bool:
        """Terminate one child only when RQ reports it idle. Safe on Windows."""
        if self.worker_state(pid) == "idle":
            try:
                self._procs[pid].terminate()
                print(f"[autoscale] stopping idle worker pid={pid}", flush=True)
                return True
            except ProcessLookupError:
                return True
        return False

    def run_once(self) -> int:
        """One poll cycle: reap, scale to target. Returns pool size."""
        self._reap()
        depth = self.queue_depth()
        target = target_worker_count(depth, self.policy)
        while self.size < target:
            self._spawn()
        # Shrink: terminate idle children beyond target. Track pending stops
        # separately (terminated procs remain in _procs until they exit).
        stopping = 0
        for pid in list(self._procs):
            if self.size - stopping <= target:
                break
            if self._terminate_idle(pid):
                stopping += 1
        return self.size

    # ---- lifecycle ----

    def _shutdown_children(self) -> None:
        for proc in list(self._procs.values()):
            try:
                proc.terminate()
            except OSError:
                pass
        deadline = time.monotonic() + 10
        while self._procs and time.monotonic() < deadline:
            self._reap()
            if not self._procs:
                break
            time.sleep(0.2)
        for proc in list(self._procs.values()):
            try:
                proc.kill()
            except OSError:
                pass

    def run(self) -> None:
        print(
            f"[autoscale] starting queue={self.settings.rq_queue} "
            f"pool=[{self.policy.min_workers}..{self.policy.max_workers}] "
            f"poll={self.policy.poll_seconds}s",
            flush=True,
        )

        def _handle_stop(_sig: int, _frame: Any) -> None:
            self._stop = True

        previous = None
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handle_stop)
            except (ValueError, OSError):  # not the main thread / unsupported
                pass

        while not self._stop:
            size = self.run_once()
            if size != previous:
                previous = size
                print(f"[autoscale] pool={size} depth={self.queue_depth()}", flush=True)
            time.sleep(self.policy.poll_seconds)
        self._shutdown_children()
        print("[autoscale] stopped", flush=True)


def main() -> int:
    policy = ScalePolicy.from_env()
    try:
        policy.validate()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pool = WorkerPool(policy)
    pool.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
