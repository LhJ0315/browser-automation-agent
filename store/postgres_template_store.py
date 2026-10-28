"""Postgres-backed TemplateStore (same surface as the file store)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, String, Text, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from browser_agent.archive import utc_now
from store.postgres_task_store import _metadata, run_migrations
from store.template_store import TemplateRecord, validate_template_name


class Base(DeclarativeBase):
    metadata = _metadata


class TemplateRow(Base):
    __tablename__ = "templates"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    match: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    variables: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


def _row_to_record(row: TemplateRow) -> TemplateRecord:
    return TemplateRecord(
        name=row.name,
        description=row.description or "",
        match=row.match or "",
        steps=list(row.steps or []),
        variables=dict(row.variables or {}),
        created_at=row.created_at or "",
        updated_at=row.updated_at or "",
        owner_id=row.owner_id,
    )


def _apply_record(row: TemplateRow, record: TemplateRecord) -> None:
    row.description = record.description or ""
    row.match = record.match or ""
    row.steps = list(record.steps or [])
    row.variables = dict(record.variables or {})
    row.owner_id = record.owner_id
    row.created_at = record.created_at
    row.updated_at = record.updated_at


class PostgresTemplateStore:
    """Same method surface as ``TemplateStore`` (file)."""

    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        self.database_url = database_url
        self.engine = engine or create_engine(database_url, pool_pre_ping=True)
        self._Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        run_migrations(database_url)

    def path_for(self, name: str) -> str:
        # Kept for interface parity; file store returns a Path.
        return validate_template_name(name)

    def list_templates(self, *, visible_to: str | None = None) -> list[TemplateRecord]:
        """``visible_to=None`` -> all; else own + global (owner_id is NULL)."""
        with self._Session() as session:
            stmt = select(TemplateRow).order_by(TemplateRow.name)
            if visible_to is not None:
                stmt = stmt.where(
                    (TemplateRow.owner_id.is_(None))
                    | (TemplateRow.owner_id == visible_to)
                )
            rows = session.scalars(stmt).all()
            return [_row_to_record(r) for r in rows]

    def get(self, name: str) -> TemplateRecord | None:
        with self._Session() as session:
            row = session.get(TemplateRow, validate_template_name(name))
            if row is None:
                return None
            return _row_to_record(row)

    def create(self, record: TemplateRecord) -> TemplateRecord:
        now = utc_now()
        record.name = validate_template_name(record.name)
        record.created_at = record.created_at or now
        record.updated_at = now
        with self._Session() as session:
            existing = session.get(TemplateRow, record.name)
            if existing is not None:
                raise FileExistsError(f"template already exists: {record.name}")
            row = TemplateRow(name=record.name)
            _apply_record(row, record)
            session.add(row)
            session.commit()
        return record

    def save(self, record: TemplateRecord) -> TemplateRecord:
        record.updated_at = utc_now()
        with self._Session() as session:
            row = session.get(TemplateRow, validate_template_name(record.name))
            if row is None:
                raise FileNotFoundError(f"template not found: {record.name}")
            _apply_record(row, record)
            session.commit()
        return record

    def update(self, name: str, **fields_value: Any) -> TemplateRecord:
        with self._Session() as session:
            row = session.get(TemplateRow, validate_template_name(name))
            if row is None:
                raise FileNotFoundError(f"template not found: {name}")
            rec = _row_to_record(row)
            for key, value in fields_value.items():
                if not hasattr(rec, key):
                    raise AttributeError(key)
                setattr(rec, key, value)
            rec.updated_at = utc_now()
            _apply_record(row, rec)
            session.commit()
            return rec

    def delete(self, name: str) -> bool:
        with self._Session() as session:
            row = session.get(TemplateRow, validate_template_name(name))
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def ping(self) -> bool:
        with self.engine.connect() as conn:
            conn.execute(select(1))
        return True
