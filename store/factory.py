"""Task / template store factories: file (default) or postgres."""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol

from settings import Settings, get_settings
from store.task_store import TaskRecord, TaskStore
from store.template_store import TemplateRecord, TemplateStore


class TaskStoreProtocol(Protocol):
    def create(self, record: TaskRecord) -> TaskRecord: ...

    def get(self, task_id: str) -> TaskRecord | None: ...

    def save(self, record: TaskRecord) -> TaskRecord: ...

    def update(self, task_id: str, **fields: object) -> TaskRecord: ...

    def is_cancel_requested(self, task_id: str) -> bool: ...

    def list_tasks(self, *, limit: int = 100, owner_id: str | None = None) -> list[TaskRecord]: ...


class TemplateStoreProtocol(Protocol):
    def list_templates(self, *, visible_to: str | None = None) -> list[TemplateRecord]: ...

    def get(self, name: str) -> TemplateRecord | None: ...

    def create(self, record: TemplateRecord) -> TemplateRecord: ...

    def save(self, record: TemplateRecord) -> TemplateRecord: ...

    def update(self, name: str, **fields: object) -> TemplateRecord: ...

    def delete(self, name: str) -> bool: ...


def build_task_store(settings: Settings | None = None) -> TaskStoreProtocol:
    cfg = settings or get_settings()
    backend = (cfg.task_store or "file").lower()
    if backend == "file":
        return TaskStore(cfg.tasks_dir)
    if backend == "postgres":
        if not cfg.database_url:
            raise RuntimeError("TASK_STORE=postgres requires DATABASE_URL")
        from store.postgres_task_store import PostgresTaskStore

        return PostgresTaskStore(cfg.database_url)
    raise ValueError(f"unknown TASK_STORE={backend!r} (expected file|postgres)")


@lru_cache(maxsize=1)
def get_task_store() -> TaskStoreProtocol:
    """Process-wide store singleton (API + worker)."""
    return build_task_store()


def reset_task_store_cache() -> None:
    """Test helper: clear the cached singleton."""
    get_task_store.cache_clear()


def build_template_store(settings: Settings | None = None) -> TemplateStoreProtocol:
    cfg = settings or get_settings()
    backend = (cfg.template_store or "file").lower()
    if backend == "file":
        return TemplateStore(cfg.templates_dir)
    if backend == "postgres":
        if not cfg.database_url:
            raise RuntimeError("TEMPLATE_STORE=postgres requires DATABASE_URL")
        from store.postgres_template_store import PostgresTemplateStore

        return PostgresTemplateStore(cfg.database_url)
    raise ValueError(f"unknown TEMPLATE_STORE={backend!r} (expected file|postgres)")


@lru_cache(maxsize=1)
def get_template_store() -> TemplateStoreProtocol:
    """Process-wide template store singleton (API + planner + worker)."""
    return build_template_store()


def reset_template_store_cache() -> None:
    """Test helper: clear the factory singleton and the legacy file singleton."""
    get_template_store.cache_clear()
    from store.template_store import reset_template_store_cache as _file_reset

    _file_reset()
