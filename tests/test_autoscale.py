"""Worker autoscaling tests: pure policy math + a real-queue integration
(skipped when Redis is unreachable)."""

from __future__ import annotations

import time

import pytest

from worker.autoscale import ScalePolicy, WorkerPool, target_worker_count

POLICY = ScalePolicy(min_workers=1, max_workers=4, jobs_per_worker=1, poll_seconds=1)


def test_target_at_or_below_min():
    for depth in (-1, 0):
        assert target_worker_count(depth, POLICY) == 1


def test_target_scales_with_depth():
    assert target_worker_count(1, POLICY) == 1
    assert target_worker_count(2, POLICY) == 2
    assert target_worker_count(4, POLICY) == 4


def test_target_capped_at_max():
    assert target_worker_count(10, POLICY) == 4
    assert target_worker_count(100, POLICY) == 4


def test_target_respects_jobs_per_worker():
    policy = ScalePolicy(min_workers=1, max_workers=4, jobs_per_worker=2)
    assert target_worker_count(2, policy) == 1
    assert target_worker_count(3, policy) == 2
    assert target_worker_count(5, policy) == 3


def test_policy_validation():
    bad = ScalePolicy(min_workers=3, max_workers=2)
    with pytest.raises(ValueError):
        bad.validate()
    ScalePolicy(min_workers=2, max_workers=2).validate()


def _redis_reachable() -> bool:
    try:
        from redis import Redis

        Redis(host="127.0.0.1", port=6379, socket_timeout=2).ping()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _redis_reachable(), reason="Redis not reachable")
def test_pool_scales_up_and_drains(tmp_path, monkeypatch):
    """Spawn a real autoscaled pool; enqueue demo tasks; expect pool growth
    and successful execution, then shrink back to min."""
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("RQ_QUEUE", "browser_tasks_autoscale_test")
    from browser_agent.archive import utc_now
    from store.factory import reset_task_store_cache
    from store.task_store import TaskRecord, TaskStore
    from worker.queue import enqueue_task, reset_queue_cache

    reset_queue_cache()
    reset_task_store_cache()
    try:
        store = TaskStore(tmp_path / "tasks")
        ids = []
        for i in range(2):
            tid = f"autoscale-{i}-{time.time_ns()}"
            store.create(
                TaskRecord(
                    task_id=tid,
                    status="queued",
                    source="demo",
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            ids.append(tid)
            enqueue_task(tid)

        policy = ScalePolicy(min_workers=1, max_workers=2, jobs_per_worker=1, poll_seconds=1)
        pool = WorkerPool(policy)
        try:
            deadline = time.monotonic() + 45
            peak = 1
            while time.monotonic() < deadline:
                pool.run_once()
                peak = max(peak, pool.size)
                if pool.queue_depth() == 0 and all(
                    pool.redis.exists(f"rq:job:{tid}") for tid in ids
                ):
                    break
                time.sleep(1)
            assert peak >= 2, f"pool never scaled up (peak={peak})"

            # Let workers drain and shrink back to min.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                pool.run_once()
                if pool.size <= policy.min_workers:
                    break
                time.sleep(1)
            assert pool.size <= policy.min_workers + 1
        finally:
            pool._shutdown_children()
    finally:
        reset_queue_cache()
