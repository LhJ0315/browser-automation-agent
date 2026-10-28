"""RQ helpers: enqueue / cancel. Isolated so tests can stub them."""

from __future__ import annotations

from typing import Any

from redis import Redis
from rq import Queue, Retry
from rq.exceptions import NoSuchJobError
from rq.job import Job

from settings import get_settings
from worker.jobs import execute_task

_redis: Redis | None = None
_queue: Queue | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url)
    return _redis


def get_queue() -> Queue:
    global _queue
    if _queue is None:
        settings = get_settings()
        _queue = Queue(settings.rq_queue, connection=get_redis())
    return _queue


def ping_redis() -> bool:
    try:
        return bool(get_redis().ping())
    except Exception:  # noqa: BLE001
        return False


def enqueue_task(task_id: str) -> str:
    settings = get_settings()
    job = get_queue().enqueue(
        execute_task,
        task_id,
        job_id=task_id,
        job_timeout=settings.max_task_seconds,
        result_ttl=86400,
        failure_ttl=86400,
        description=f"browser task {task_id}",
        retry=Retry(max=1, interval=5),
    )
    return str(job.id)


def cancel_queued_job(job_id: str) -> bool:
    """Cancel a queued (not started) RQ job. Returns True if cancelled or already gone."""
    try:
        job = Job.fetch(job_id, connection=get_redis())
    except NoSuchJobError:
        return True
    try:
        job.cancel()
        return True
    except Exception:  # noqa: BLE001
        return False


def reset_queue_cache() -> None:
    """Test helper."""
    global _redis, _queue
    _redis = None
    _queue = None
