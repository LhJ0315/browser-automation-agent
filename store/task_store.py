"""File-backed task metadata store (`data/tasks/{id}.json`)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from browser_agent.archive import utc_now

STATUSES = frozenset(
    {"pending", "queued", "running", "succeeded", "failed", "cancelled"}
)
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


@dataclass
class TaskRecord:
    task_id: str
    status: str = "pending"
    source: str = "demo"  # demo | task_nl | json_plan
    task_nl: str | None = None
    json_plan: dict[str, Any] | None = None
    headed: bool = False
    max_replans: int = 0
    rq_job_id: str | None = None
    run_id: str | None = None
    run_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    summary: str | None = None
    cancel_requested: bool = False
    # Optional s3://bucket/runs/{run_id} after artifact sync (M3).
    artifact_uri: str | None = None
    # Optional per-task webhook URL (overrides global WEBHOOK_URL).
    webhook_url: str | None = None
    # Multi-tenant: owning user_id (None = created via static/legacy path).
    owner_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRecord":
        known = {f.name for f in fields(cls)}
        payload = {k: v for k, v in data.items() if k in known}
        payload.setdefault("run_ids", [])
        return cls(**payload)


class TaskStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, task_id: str) -> Path:
        if not task_id or "/" in task_id or "\\" in task_id or ".." in task_id:
            raise ValueError(f"invalid task_id: {task_id!r}")
        return self.root / f"{task_id}.json"

    def create(self, record: TaskRecord) -> TaskRecord:
        now = utc_now()
        record.created_at = record.created_at or now
        record.updated_at = now
        dest = self.path_for(record.task_id)
        if dest.exists():
            raise FileExistsError(f"task already exists: {record.task_id}")
        self._write(dest, record.to_dict())
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        path = self.path_for(task_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return TaskRecord.from_dict(data)

    def save(self, record: TaskRecord) -> TaskRecord:
        record.updated_at = utc_now()
        self._write(self.path_for(record.task_id), record.to_dict())
        return record

    def update(self, task_id: str, **fields: Any) -> TaskRecord:
        rec = self.get(task_id)
        if rec is None:
            raise FileNotFoundError(f"task not found: {task_id}")
        for key, value in fields.items():
            if not hasattr(rec, key):
                raise AttributeError(key)
            setattr(rec, key, value)
        if rec.status not in STATUSES:
            raise ValueError(f"invalid status: {rec.status}")
        return self.save(rec)

    def is_cancel_requested(self, task_id: str) -> bool:
        rec = self.get(task_id)
        if rec is None:
            return True
        return bool(rec.cancel_requested) or rec.status == "cancelled"

    def list_tasks(self, *, limit: int = 100, owner_id: str | None = None) -> list[TaskRecord]:
        records: list[TaskRecord] = []
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                records.append(TaskRecord.from_dict(data))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        if owner_id is not None:
            records = [r for r in records if r.owner_id == owner_id]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[: max(0, int(limit))]

    def _write(self, dest: Path, payload: dict[str, Any]) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=dest.name, suffix=".tmp", dir=dest.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp_name, dest)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
