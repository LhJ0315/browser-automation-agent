"""Historical file-store -> Postgres migration tests (dedicated DB)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from browser_agent.archive import utc_now


def _postgres_reachable() -> bool:
    try:
        import psycopg

        psycopg.connect(
            "postgresql://browser:browser@127.0.0.1:5432/browser_agent",
            connect_timeout=2,
        ).close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _drop_database_if_exists(name: str) -> None:
    import psycopg

    with psycopg.connect(
        "postgresql://browser:browser@127.0.0.1:5432/browser_agent",
        autocommit=True,
    ) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _create_database(name: str) -> None:
    import psycopg

    with psycopg.connect(
        "postgresql://browser:browser@127.0.0.1:5432/browser_agent",
        autocommit=True,
    ) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')


def _write_task(tasks_dir: Path, rec: dict) -> None:
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / f"{rec['task_id']}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@pytest.mark.skipif(not _postgres_reachable(), reason="Postgres container not reachable")
def test_migrate_tasks_import_idempotent(tmp_path):
    db_name = "browser_agent_migrate_test"
    url = "postgresql+psycopg://browser:browser@127.0.0.1:5432/" + db_name
    _drop_database_if_exists(db_name)
    _create_database(db_name)
    try:
        from scripts.migrate_tasks import _import, _source_records

        # seed three historical tasks (oldest first)
        tasks_dir = tmp_path / "tasks"
        base = "2026-08-01T00:00:00Z"
        for i in range(3):
            _write_task(
                tasks_dir,
                {
                    "task_id": f"hist-{i}",
                    "status": "succeeded",
                    "source": "demo",
                    "created_at": base,
                    "updated_at": base,
                    "run_id": f"run-hist-{i}",
                    "run_ids": [f"run-hist-{i}"],
                    "summary": f"old task {i}",
                },
            )

        records = _source_records(tasks_dir, None)
        assert len(records) == 3

        # first import: all created
        stats = _import(records, database_url=url, overwrite=False)
        assert stats["imported"] == 3 and stats["skipped"] == 0

        # second import: all skipped (idempotent)
        stats2 = _import(records, database_url=url, overwrite=False)
        assert stats2["imported"] == 0 and stats2["skipped"] == 3

        # overwrite: updates rows
        stats3 = _import(records, database_url=url, overwrite=True)
        assert stats3["imported"] == 3 and stats3["skipped"] == 0

        # spot-check persisted values
        from store.postgres_task_store import PostgresTaskStore

        store = PostgresTaskStore(url)
        rec = store.get("hist-2")
        assert rec is not None
        assert rec.source == "demo"
        assert rec.run_id == "run-hist-2"
        assert rec.created_at == base
    finally:
        _drop_database_if_exists(db_name)
