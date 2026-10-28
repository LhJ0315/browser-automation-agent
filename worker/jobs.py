"""RQ job: run a stored browser task in the worker process."""

from __future__ import annotations

from typing import Any

from browser_agent.service import RunRequest, run_plan
from settings import get_settings
from store.artifact_store import get_artifact_store
from store.factory import get_task_store
from store.task_store import TERMINAL


def _store():
    return get_task_store()


def _notify_terminal(store: Any, task_id: str) -> None:
    """Fire terminal-event webhook; record failures on the task (non-fatal)."""
    try:
        from worker.webhook import notify_task_terminal

        rec = store.get(task_id)
        if rec is None:
            return
        outcome = notify_task_terminal(rec)
        if outcome.get("sent"):
            return
        detail = outcome.get("reason")
        if not detail:
            failed = [r for r in outcome.get("results", []) if not r.get("ok")]
            detail = "; ".join(f"{r['url']}: {r['detail']}" for r in failed)
        if detail:
            base = rec.error or ""
            store.update(
                task_id,
                error=(base + " | " if base else "") + f"webhook failed: {detail}",
            )
    except Exception:  # noqa: BLE001 — webhook must never break the task lifecycle
        pass


def execute_task(task_id: str) -> dict[str, Any]:
    """RQ entrypoint. All Playwright + LLM work happens here."""
    settings = get_settings()
    store = _store()
    rec = store.get(task_id)
    if rec is None:
        return {"task_id": task_id, "status": "failed", "error": "task not found"}

    if rec.cancel_requested or rec.status == "cancelled":
        store.update(
            task_id,
            status="cancelled",
            finished_at=rec.finished_at,
            summary=rec.summary or "cancelled",
        )
        _notify_terminal(store, task_id)
        return {"task_id": task_id, "status": "cancelled"}

    if rec.status in TERMINAL:
        return {"task_id": task_id, "status": rec.status}

    from browser_agent.archive import utc_now

    store.update(task_id, status="running", started_at=utc_now())

    req = RunRequest(
        demo=rec.source == "demo",
        agent=rec.source == "agent",
        task=rec.task_nl if rec.source in ("task_nl", "agent") else None,
        # json_plan carries the plan for both raw json_plan and rendered templates.
        plan=rec.json_plan if rec.source in ("json_plan", "template") else None,
        output_root=settings.runs_root,
        headed=bool(rec.headed),
        max_replans=int(rec.max_replans or 0),
        cancel_check=lambda: store.is_cancel_requested(task_id),
        meta_extra={"task_id": task_id},
    )

    try:
        result = run_plan(req)
    except Exception as exc:  # noqa: BLE001
        store.update(
            task_id,
            status="failed",
            finished_at=utc_now(),
            error=f"{type(exc).__name__}: {exc}",
            summary=str(exc),
        )
        _notify_terminal(store, task_id)
        return {"task_id": task_id, "status": "failed", "error": str(exc)}

    if result.cancelled or store.is_cancel_requested(task_id):
        status = "cancelled"
        summary = result.summary or "cancelled"
    elif result.success:
        status = "succeeded"
        summary = result.summary
    else:
        status = "failed"
        summary = result.summary

    store.update(
        task_id,
        status=status,
        run_id=result.run_id,
        run_ids=result.run_ids,
        finished_at=utc_now(),
        summary=summary,
        error=result.error,
    )
    _notify_terminal(store, task_id)

    # Sync run artifacts (report + screenshots) to the configured artifact
    # store. No-op for LocalArtifactStore; uploads to S3/MinIO otherwise.
    # Per design: browser run finished but artifact sync failed -> keep the
    # terminal status and record the sync error on the task for visibility.
    if result.run_id:
        try:
            local_run_dir = settings.runs_root / result.run_id
            get_artifact_store().upload_run(result.run_id, local_run_dir)
        except Exception as sync_exc:  # noqa: BLE001
            store.update(
                task_id,
                error=(
                    f"run finished; artifact sync failed: "
                    f"{type(sync_exc).__name__}: {sync_exc}"
                ),
            )

    return {
        "task_id": task_id,
        "status": status,
        "run_id": result.run_id,
        "summary": summary,
    }
