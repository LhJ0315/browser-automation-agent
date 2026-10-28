"""Import historical file-store tasks (``data/tasks/*.json``) into Postgres.

Idempotent by default: tasks already present in Postgres are skipped unless
``--overwrite`` is passed. Use ``--dry-run`` to preview without writing.

Examples:
    python -m scripts.migrate_tasks --dry-run
    python -m scripts.migrate_tasks                  # import all, skip existing
    python -m scripts.migrate_tasks --overwrite      # overwrite existing rows
    python -m scripts.migrate_tasks --from-dir X --limit 100
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from settings import get_settings
from store.task_store import TaskRecord, TaskStore


def _source_records(tasks_dir: Path, limit: int | None) -> list[TaskRecord]:
    store = TaskStore(tasks_dir)
    records = store.list_tasks(limit=limit or 10**6)
    # File store lists newest-first; import oldest-first to keep history order.
    records.sort(key=lambda r: r.created_at or "")
    return records


def _import(records: list[TaskRecord], *, database_url: str, overwrite: bool) -> dict[str, int]:
    from store.postgres_task_store import PostgresTaskStore

    store = PostgresTaskStore(database_url)
    stats = {"total": len(records), "imported": 0, "skipped": 0, "failed": 0}
    failures: list[tuple[str, str]] = []
    for rec in records:
        try:
            existing = store.get(rec.task_id)
            if existing is not None and not overwrite:
                stats["skipped"] += 1
                continue
            if existing is not None:
                store.save(rec)
            else:
                store.create(rec)
            stats["imported"] += 1
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            failures.append((rec.task_id, f"{type(exc).__name__}: {exc}"))
    for task_id, err in failures[:10]:
        print(f"  !! {task_id}: {err}", file=sys.stderr)
    if len(failures) > 10:
        print(f"  ... and {len(failures) - 10} more failures", file=sys.stderr)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    parser.add_argument("--overwrite", action="store_true", help="overwrite existing rows")
    parser.add_argument("--limit", type=int, default=None, help="max records to import")
    parser.add_argument(
        "--from-dir",
        type=Path,
        default=None,
        help="source tasks dir (default: settings.tasks_dir)",
    )
    args = parser.parse_args()

    database_url = get_settings().database_url
    if not database_url:
        print(
            "error: DATABASE_URL is not set; Postgres target required.",
            file=sys.stderr,
        )
        return 2

    tasks_dir = args.from_dir or get_settings().tasks_dir
    if not tasks_dir.is_dir():
        print(f"error: source dir not found: {tasks_dir}", file=sys.stderr)
        return 2

    records = _source_records(tasks_dir, args.limit)
    print(
        f"[migrate] source={tasks_dir} records={len(records)} "
        f"target={database_url.split('@')[-1]} overwrite={args.overwrite}"
    )
    if not records:
        print("[migrate] nothing to do")
        return 0

    if args.dry_run:
        for rec in records[:10]:
            print(f"  would-import {rec.task_id} {rec.status} {rec.created_at}")
        if len(records) > 10:
            print(f"  ... and {len(records) - 10} more")
        print(f"[migrate] dry-run: {len(records)} records would be imported")
        return 0

    stats = _import(records, database_url=database_url, overwrite=args.overwrite)
    print(
        f"[migrate] done: imported={stats['imported']} skipped={stats['skipped']} "
        f"failed={stats['failed']} (total={stats['total']})"
    )
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
