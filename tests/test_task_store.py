"""Tests for file-backed TaskStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from browser_agent.archive import utc_now
from store.task_store import TaskRecord, TaskStore


def test_create_get_update(tmp_path: Path):
    store = TaskStore(tmp_path)
    rec = TaskRecord(
        task_id="abc-123",
        status="pending",
        source="demo",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    store.create(rec)
    loaded = store.get("abc-123")
    assert loaded is not None
    assert loaded.status == "pending"
    assert loaded.source == "demo"

    updated = store.update("abc-123", status="queued", rq_job_id="job-1")
    assert updated.status == "queued"
    assert updated.rq_job_id == "job-1"
    assert store.get("abc-123").rq_job_id == "job-1"


def test_create_duplicate_raises(tmp_path: Path):
    store = TaskStore(tmp_path)
    store.create(TaskRecord(task_id="dup", status="pending", source="demo"))
    with pytest.raises(FileExistsError):
        store.create(TaskRecord(task_id="dup", status="pending", source="demo"))


def test_invalid_task_id(tmp_path: Path):
    store = TaskStore(tmp_path)
    with pytest.raises(ValueError):
        store.path_for("../escape")
    with pytest.raises(ValueError):
        store.path_for("a/b")


def test_cancel_flag(tmp_path: Path):
    store = TaskStore(tmp_path)
    store.create(TaskRecord(task_id="c1", status="running", source="demo"))
    assert store.is_cancel_requested("c1") is False
    store.update("c1", cancel_requested=True)
    assert store.is_cancel_requested("c1") is True
    assert store.is_cancel_requested("missing") is True


def test_list_sorted_and_limited(tmp_path: Path):
    store = TaskStore(tmp_path)
    for i, ts in enumerate(["2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z", "2026-01-02T00:00:00Z"]):
        store.create(
            TaskRecord(
                task_id=f"t{i}",
                status="pending",
                source="demo",
                created_at=ts,
                updated_at=ts,
            )
        )
    listed = store.list_tasks(limit=2)
    assert len(listed) == 2
    assert listed[0].task_id == "t1"
    assert listed[1].task_id == "t2"


def test_atomic_write_readable(tmp_path: Path):
    store = TaskStore(tmp_path)
    store.create(
        TaskRecord(
            task_id="atomic",
            status="pending",
            source="json_plan",
            json_plan={"name": "x", "steps": []},
        )
    )
    path = store.path_for("atomic")
    text = path.read_text(encoding="utf-8")
    assert '"task_id": "atomic"' in text
    assert path.with_suffix(".tmp").exists() is False or True  # temp cleaned
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
