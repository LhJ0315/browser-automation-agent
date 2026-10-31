"""Task HTTP routes."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse, StreamingResponse

from api.auth import get_principal
from api.schemas import (
    CancelResponse,
    ScreenshotInfo,
    TaskCreateRequest,
    TaskCreateResponse,
    TaskDetail,
    TaskSummary,
)
from api.security import Principal, require_can_write
from browser_agent.archive import utc_now
from settings import get_settings
from store.artifact_store import get_artifact_store
from store.factory import get_task_store
from store.task_store import TERMINAL, TaskRecord
from worker.queue import cancel_queued_job, enqueue_task

router = APIRouter(prefix="/api/v1", dependencies=[Depends(get_principal)])


def _store():
    return get_task_store()


def _owner_for(principal: Principal) -> str | None:
    return None if principal.sees_all else principal.principal_id


def _ensure_visible(rec: TaskRecord | None, principal: Principal) -> TaskRecord:
    if rec is None:
        raise HTTPException(status_code=404, detail="task not found")
    if principal.sees_all or rec.owner_id == principal.principal_id:
        return rec
    raise HTTPException(status_code=404, detail="task not found")


def _store():
    return get_task_store()


def _source_of(body: TaskCreateRequest) -> str:
    if body.agent:
        return "agent"
    if body.demo:
        return "demo"
    if body.task_nl:
        return "task_nl"
    return "json_plan"


def _read_run_meta(run_id: str | None) -> dict[str, Any] | None:
    if not run_id:
        return None
    meta_path = get_settings().runs_root / run_id / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _to_summary(rec: TaskRecord) -> TaskSummary:
    return TaskSummary(
        task_id=rec.task_id,
        status=rec.status,
        source=rec.source,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
        run_id=rec.run_id,
        summary=rec.summary,
        cancel_requested=rec.cancel_requested,
    )


def _to_detail(rec: TaskRecord) -> TaskDetail:
    meta = _read_run_meta(rec.run_id)
    step_count = None
    progress_summary = None
    if meta:
        step_count = meta.get("step_count")
        progress_summary = meta.get("summary")
    return TaskDetail(
        **_to_summary(rec).model_dump(),
        task_nl=rec.task_nl,
        headed=rec.headed,
        max_replans=rec.max_replans,
        rq_job_id=rec.rq_job_id,
        run_ids=list(rec.run_ids or []),
        started_at=rec.started_at,
        finished_at=rec.finished_at,
        error=rec.error,
        step_count=step_count,
        progress_summary=progress_summary,
    )


def _run_dir_for(rec: TaskRecord) -> Path:
    if not rec.run_id:
        raise HTTPException(status_code=409, detail="run artifacts not ready")
    run_dir = (get_settings().runs_root / rec.run_id).resolve()
    root = get_settings().runs_root.resolve()
    if root not in run_dir.parents and run_dir != root:
        raise HTTPException(status_code=400, detail="invalid run path")
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="run directory missing")
    return run_dir


@router.post("/tasks", response_model=TaskCreateResponse)
def create_task(
    body: TaskCreateRequest, principal: Principal = Depends(get_principal)
) -> TaskCreateResponse:
    require_can_write(principal)
    store = _store()
    task_id = str(uuid.uuid4())
    # enqueue_task reuses task_id as the RQ job id; persist it before enqueue so
    # the worker's "running" update cannot be overwritten by a late queued write.
    rec = TaskRecord(
        task_id=task_id,
        status="queued",
        rq_job_id=task_id,
        source=_source_of(body),
        task_nl=body.task_nl,
        json_plan=body.json_plan,
        headed=bool(body.headed),
        max_replans=int(body.max_replans),
        webhook_url=(body.webhook_url or "").strip() or None,
        owner_id=_owner_for(principal),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    store.create(rec)
    try:
        job_id = enqueue_task(task_id)
    except Exception as exc:  # noqa: BLE001
        store.update(
            task_id,
            status="failed",
            finished_at=utc_now(),
            error=f"enqueue failed: {exc}",
            summary=f"enqueue failed: {exc}",
        )
        raise HTTPException(status_code=503, detail=f"failed to enqueue task: {exc}") from exc

    return TaskCreateResponse(task_id=task_id, status="queued", rq_job_id=job_id)


@router.get("/tasks", response_model=list[TaskSummary])
def list_tasks(
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(get_principal),
) -> list[TaskSummary]:
    return [
        _to_summary(r)
        for r in _store().list_tasks(
            limit=limit, owner_id=_owner_for(principal)
        )
    ]


@router.get("/tasks/{task_id}", response_model=TaskDetail)
def get_task(
    task_id: str, principal: Principal = Depends(get_principal)
) -> TaskDetail:
    rec = _ensure_visible(_store().get(task_id), principal)
    return _to_detail(rec)


@router.post("/tasks/{task_id}/cancel", response_model=CancelResponse)
def cancel_task(
    task_id: str, principal: Principal = Depends(get_principal)
) -> CancelResponse:
    require_can_write(principal)
    store = _store()
    rec = _ensure_visible(store.get(task_id), principal)

    if rec.status in TERMINAL:
        return CancelResponse(
            task_id=task_id,
            status=rec.status,
            cancel_requested=True,
            detail="task already finished",
        )

    store.update(task_id, cancel_requested=True)

    detail = "cancel requested"
    if rec.status in {"pending", "queued"} and rec.rq_job_id:
        cancel_queued_job(rec.rq_job_id)
        store.update(
            task_id,
            status="cancelled",
            finished_at=utc_now(),
            summary="cancelled before start",
        )
        detail = "cancelled before worker start"
        status = "cancelled"
    else:
        # running: cooperative cancel between steps
        status = "running"
        detail = "cancel requested; worker will stop between steps"

    rec2 = store.get(task_id)
    return CancelResponse(
        task_id=task_id,
        status=(rec2.status if rec2 else status),
        cancel_requested=True,
        detail=detail,
    )


@router.get("/tasks/{task_id}/report")
def get_report(
    task_id: str, principal: Principal = Depends(get_principal)
) -> Response:
    rec = _ensure_visible(_store().get(task_id), principal)
    if rec.status not in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="report not ready")
    if not rec.run_id:
        raise HTTPException(status_code=404, detail="report.md missing")
    body = get_artifact_store().get_report(rec.run_id)
    if body is None:
        raise HTTPException(status_code=404, detail="report.md missing")
    return Response(content=body, media_type="text/markdown; charset=utf-8")


@router.get("/tasks/{task_id}/steps")
def get_steps(
    task_id: str, principal: Principal = Depends(get_principal)
) -> dict[str, Any]:
    """Return the per-step trajectory from meta.json (agent loop especially)."""
    rec = _ensure_visible(_store().get(task_id), principal)
    if not rec.run_id:
        return {"steps": [], "mode": None, "goal": None}
    meta_path = get_settings().runs_root / rec.run_id / "meta.json"
    if not meta_path.is_file():
        return {"steps": [], "mode": None, "goal": None}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"steps": [], "mode": None, "goal": None}
    return {
        "mode": data.get("mode"),
        "goal": data.get("goal"),
        "step_count": data.get("step_count"),
        "steps": data.get("steps", []),
    }


@router.get("/tasks/{task_id}/stream")
async def stream_task(
    task_id: str, principal: Principal = Depends(get_principal)
) -> StreamingResponse:
    """SSE stream of task status + new steps as they happen.

    Event types:
      - status: {status, run_id, summary}
      - step:   {step: {...}}  (one per new step observed in meta.json)
      - done:   final terminal status, then the stream closes.
    """
    rec = _ensure_visible(_store().get(task_id), principal)
    store = _store()
    settings = get_settings()

    async def gen():
        seen_steps = 0
        last_status = ""
        for _ in range(600):  # up to ~10 minutes at 1s interval
            rec = store.get(task_id)
            if rec.status != last_status:
                last_status = rec.status
                yield f"event: status\ndata: {json.dumps({'status': rec.status, 'run_id': rec.run_id, 'summary': rec.summary})}\n\n"
            if rec.run_id:
                meta_path = settings.runs_root / rec.run_id / "meta.json"
                if meta_path.is_file():
                    try:
                        data = json.loads(meta_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        data = {}
                    steps = data.get("steps", []) or []
                    while seen_steps < len(steps):
                        yield f"event: step\ndata: {json.dumps(steps[seen_steps])}\n\n"
                        seen_steps += 1
            if rec.status in {"succeeded", "failed", "cancelled"}:
                yield f"event: done\ndata: {json.dumps({'status': rec.status, 'run_id': rec.run_id, 'summary': rec.summary})}\n\n"
                return
            await asyncio.sleep(1.0)
        yield "event: done\ndata: {\"status\": \"timeout\"}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tasks/{task_id}/screenshots", response_model=list[ScreenshotInfo])
def list_screenshots(
    task_id: str, principal: Principal = Depends(get_principal)
) -> list[ScreenshotInfo]:
    rec = _ensure_visible(_store().get(task_id), principal)
    if not rec.run_id:
        return []
    metas = get_artifact_store().list_screenshots(rec.run_id)
    return [ScreenshotInfo(name=m.name, path=m.path) for m in metas]


@router.get("/tasks/{task_id}/screenshots/{name}")
def get_screenshot(
    task_id: str, name: str, principal: Principal = Depends(get_principal)
) -> Response:
    rec = _ensure_visible(_store().get(task_id), principal)
    if not rec.run_id:
        raise HTTPException(status_code=404, detail="screenshot not found")
    try:
        result = get_artifact_store().open_screenshot(rec.run_id, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="screenshot not found")
    body, content_type = result
    return Response(content=body, media_type=content_type)
