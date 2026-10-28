"""Postgres-backed TaskStore (SQLAlchemy 2.x + Alembic migrations)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    Integer,
    MetaData,
    String,
    Text,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from browser_agent.archive import utc_now
from store.task_store import STATUSES, TaskRecord

_metadata = MetaData()


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def run_migrations(database_url: str) -> None:
    """Apply Alembic migrations idempotently.

    - Fresh database:  ``upgrade head`` (creates tasks + webhook_url column).
    - Legacy table from ``create_all`` (no alembic_version): stamp the
      baseline revision, then ``upgrade head`` (adds webhook_url).
    - Already migrated: no-op.
    """
    from alembic import command
    from alembic.config import Config

    root = _package_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            has_version = inspect(conn).has_table("alembic_version")
            has_tasks = inspect(conn).has_table("tasks")
        if not has_version and has_tasks:
            command.stamp(cfg, "0001")
        command.upgrade(cfg, "head")
    finally:
        engine.dispose()


class Base(DeclarativeBase):
    metadata = _metadata


class TaskRow(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="demo")
    task_nl: Mapped[str | None] = mapped_column(Text, nullable=True)
    json_plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    headed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_replans: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rq_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    run_ids: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    artifact_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


def _row_to_record(row: TaskRow) -> TaskRecord:
    return TaskRecord(
        task_id=row.task_id,
        status=row.status,
        source=row.source,
        task_nl=row.task_nl,
        json_plan=row.json_plan,
        headed=bool(row.headed),
        max_replans=int(row.max_replans or 0),
        rq_job_id=row.rq_job_id,
        run_id=row.run_id,
        run_ids=list(row.run_ids or []),
        created_at=row.created_at or "",
        updated_at=row.updated_at or "",
        started_at=row.started_at,
        finished_at=row.finished_at,
        error=row.error,
        summary=row.summary,
        cancel_requested=bool(row.cancel_requested),
        artifact_uri=row.artifact_uri,
        webhook_url=row.webhook_url,
        owner_id=row.owner_id,
    )


def _apply_record(row: TaskRow, record: TaskRecord) -> None:
    row.status = record.status
    row.source = record.source
    row.task_nl = record.task_nl
    row.json_plan = record.json_plan
    row.headed = bool(record.headed)
    row.max_replans = int(record.max_replans or 0)
    row.rq_job_id = record.rq_job_id
    row.run_id = record.run_id
    row.run_ids = list(record.run_ids or [])
    row.created_at = record.created_at
    row.updated_at = record.updated_at
    row.started_at = record.started_at
    row.finished_at = record.finished_at
    row.error = record.error
    row.summary = record.summary
    row.cancel_requested = bool(record.cancel_requested)
    row.artifact_uri = record.artifact_uri
    row.webhook_url = record.webhook_url
    row.owner_id = record.owner_id


class PostgresTaskStore:
    """Same method surface as ``TaskStore`` (file)."""

    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        self.database_url = database_url
        self.engine = engine or create_engine(database_url, pool_pre_ping=True)
        self._Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        run_migrations(database_url)

    def create(self, record: TaskRecord) -> TaskRecord:
        now = utc_now()
        record.created_at = record.created_at or now
        record.updated_at = now
        with self._Session() as session:
            existing = session.get(TaskRow, record.task_id)
            if existing is not None:
                raise FileExistsError(f"task already exists: {record.task_id}")
            row = TaskRow(task_id=record.task_id)
            _apply_record(row, record)
            session.add(row)
            session.commit()
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        with self._Session() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                return None
            return _row_to_record(row)

    def save(self, record: TaskRecord) -> TaskRecord:
        record.updated_at = utc_now()
        with self._Session() as session:
            row = session.get(TaskRow, record.task_id)
            if row is None:
                raise FileNotFoundError(f"task not found: {record.task_id}")
            _apply_record(row, record)
            session.commit()
        return record

    def update(self, task_id: str, **fields: Any) -> TaskRecord:
        with self._Session() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise FileNotFoundError(f"task not found: {task_id}")
            rec = _row_to_record(row)
            for key, value in fields.items():
                if not hasattr(rec, key):
                    raise AttributeError(key)
                setattr(rec, key, value)
            if rec.status not in STATUSES:
                raise ValueError(f"invalid status: {rec.status}")
            rec.updated_at = utc_now()
            _apply_record(row, rec)
            session.commit()
            return rec

    def is_cancel_requested(self, task_id: str) -> bool:
        with self._Session() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                return True
            return bool(row.cancel_requested) or row.status == "cancelled"

    def list_tasks(self, *, limit: int = 100, owner_id: str | None = None) -> list[TaskRecord]:
        lim = max(0, int(limit))
        with self._Session() as session:
            stmt = select(TaskRow).order_by(TaskRow.created_at.desc()).limit(lim)
            if owner_id is not None:
                stmt = stmt.where(TaskRow.owner_id == owner_id)
            rows = session.scalars(stmt).all()
            return [_row_to_record(r) for r in rows]

    def ping(self) -> bool:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
