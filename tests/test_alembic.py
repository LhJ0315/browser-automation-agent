"""Alembic migration tests.

- Offline SQL generation (no DB needed) verifies both revisions compile.
- A dedicated-database integration test runs ``upgrade head`` + ``downgrade base``
  against real Postgres; it skips when the container is not reachable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent

TEST_DATABASE_URL = (
    "postgresql+psycopg://browser:browser@127.0.0.1:5432/browser_agent_test"
)


def _cfg(**overrides) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    for k, v in overrides.items():
        cfg.set_main_option(k, v)
    return cfg


def test_offline_sql_covers_both_revisions(capsys):
    cfg = _cfg(**{"sqlalchemy.url": "postgresql+psycopg://u:p@h/db"})
    # upgrade head in offline mode prints DDL to stdout
    command.upgrade(cfg, "head", sql=True)
    sql = capsys.readouterr().out
    assert "CREATE TABLE tasks" in sql
    assert "webhook_url" in sql
    assert "ix_tasks_status" in sql

    # downgrade back to base also compiles
    command.downgrade(cfg, "head:base", sql=True)
    assert "DROP TABLE tasks" in capsys.readouterr().out


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


@pytest.mark.skipif(not _postgres_reachable(), reason="Postgres container not reachable")
def test_upgrade_and_downgrade_on_dedicated_db():
    db_name = "browser_agent_test"
    _drop_database_if_exists(db_name)
    _create_database(db_name)
    try:
        cfg = _cfg(**{"sqlalchemy.url": TEST_DATABASE_URL})
        command.upgrade(cfg, "head")

        import sqlalchemy as sa

        engine = sa.create_engine(TEST_DATABASE_URL)
        try:
            insp = sa.inspect(engine)
            cols = {c["name"]: c for c in insp.get_columns("tasks")}
            assert "webhook_url" in cols
            assert "artifact_uri" in cols
            indexes = {ix["name"] for ix in insp.get_indexes("tasks")}
            assert "ix_tasks_status" in indexes
            assert "ix_tasks_created_at" in indexes
        finally:
            engine.dispose()

        command.downgrade(cfg, "base")
        engine2 = sa.create_engine(TEST_DATABASE_URL)
        try:
            assert not sa.inspect(engine2).has_table("tasks")
        finally:
            engine2.dispose()
    finally:
        _drop_database_if_exists(db_name)
