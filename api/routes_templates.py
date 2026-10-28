"""Plan template CRUD + run HTTP routes (/api/v1/templates)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from api.auth import get_principal
from api.schemas import (
    TaskCreateResponse,
    TemplateDetail,
    TemplatePayload,
    TemplateRunRequest,
    TemplateSummary,
    TemplateUpdate,
)
from api.security import Principal, require_can_write
from browser_agent.archive import utc_now
from browser_agent.schema import PlanValidationError, parse_plan
from store.factory import get_task_store, get_template_store
from store.template_render import render_template
from store.template_store import TemplateRecord
from store.task_store import TaskRecord
from worker.queue import enqueue_task

router = APIRouter(
    prefix="/api/v1/templates",
    dependencies=[Depends(get_principal)],
)


def _store():
    return get_template_store()


def _owner_for(principal: Principal) -> str | None:
    # Users own their templates; admin/static creates global (shared) ones.
    return None if principal.sees_all else principal.principal_id


def _ensure_visible(rec: TemplateRecord | None, principal: Principal) -> TemplateRecord:
    if rec is None:
        raise HTTPException(status_code=404, detail="template not found")
    if principal.sees_all or rec.owner_id is None or rec.owner_id == principal.principal_id:
        return rec
    raise HTTPException(status_code=404, detail="template not found")


def _ensure_editable(rec: TemplateRecord, principal: Principal) -> None:
    require_can_write(principal)
    if principal.sees_all or rec.owner_id == principal.principal_id:
        return
    # Global templates are read-only for non-owners.
    raise HTTPException(
        status_code=403,
        detail="global templates are read-only for non-admin users",
    )


def _to_summary(rec: TemplateRecord) -> TemplateSummary:
    return TemplateSummary(
        name=rec.name,
        description=rec.description,
        match=rec.match,
        step_count=len(rec.steps or []),
        variable_count=len(rec.variables or {}),
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


def _to_detail(rec: TemplateRecord) -> TemplateDetail:
    return TemplateDetail(
        **_to_summary(rec).model_dump(),
        steps=list(rec.steps or []),
        variables=dict(rec.variables or {}),
    )


@router.get("", response_model=list[TemplateSummary])
def list_templates(
    principal: Principal = Depends(get_principal),
) -> list[TemplateSummary]:
    visible_to = None if principal.sees_all else principal.principal_id
    return [
        _to_summary(r)
        for r in _store().list_templates(visible_to=visible_to)
    ]


@router.post("", response_model=TemplateDetail, status_code=201)
def create_template(
    body: TemplatePayload, principal: Principal = Depends(get_principal)
) -> TemplateDetail:
    require_can_write(principal)
    store = _store()
    try:
        rec = TemplateRecord(
            name=body.name,
            description=body.description.strip(),
            match=body.match.strip(),
            steps=body.steps,
            variables=dict(body.variables or {}),
            owner_id=_owner_for(principal),
        )
        store.create(rec)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _to_detail(rec)


@router.get("/{name}", response_model=TemplateDetail)
def get_template(
    name: str, principal: Principal = Depends(get_principal)
) -> TemplateDetail:
    rec = _ensure_visible(_store().get(name), principal)
    return _to_detail(rec)


@router.put("/{name}", response_model=TemplateDetail)
def update_template(
    name: str,
    body: TemplateUpdate,
    principal: Principal = Depends(get_principal),
) -> TemplateDetail:
    store = _store()
    rec = _ensure_visible(store.get(name), principal)
    _ensure_editable(rec, principal)
    updates: dict[str, object] = {}
    if body.description is not None:
        updates["description"] = body.description.strip()
    if body.match is not None:
        updates["match"] = body.match.strip()
    if body.steps is not None:
        try:
            parse_plan({"name": name, "steps": body.steps}, source="template")
        except PlanValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        updates["steps"] = body.steps
    if body.variables is not None:
        updates["variables"] = dict(body.variables)
    try:
        rec = store.update(name, **updates)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _to_detail(rec)


@router.delete("/{name}", status_code=204)
def delete_template(
    name: str, principal: Principal = Depends(get_principal)
) -> None:
    store = _store()
    rec = _ensure_visible(store.get(name), principal)
    _ensure_editable(rec, principal)
    if not store.delete(name):
        raise HTTPException(status_code=404, detail="template not found")


@router.post("/{name}/run", response_model=TaskCreateResponse)
def run_template(
    name: str,
    body: TemplateRunRequest,
    principal: Principal = Depends(get_principal),
) -> TaskCreateResponse:
    """Render the template's steps (filling ``{{var}}`` placeholders) and
    enqueue a task from the result."""
    require_can_write(principal)
    store = _store()
    rec = _ensure_visible(store.get(name), principal)

    rendered, missing = render_template(
        list(rec.steps or []),
        dict(body.variables or {}),
        defaults=dict(rec.variables or {}),
    )
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"missing template variables: {', '.join(missing)}",
        )
    try:
        parse_plan(
            {"name": rec.name, "description": rec.description, "steps": rendered},
            source="template",
        )
    except PlanValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task_store = get_task_store()
    task_id = str(uuid.uuid4())
    rec_task = TaskRecord(
        task_id=task_id,
        status="pending",
        source="template",
        json_plan={
            "name": rec.name,
            "description": rec.description,
            "source": "template",
            "steps": rendered,
        },
        headed=bool(body.headed),
        max_replans=int(body.max_replans),
        webhook_url=(body.webhook_url or "").strip() or None,
        owner_id=_owner_for(principal),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    task_store.create(rec_task)
    try:
        job_id = enqueue_task(task_id)
    except Exception as exc:  # noqa: BLE001
        task_store.update(
            task_id,
            status="failed",
            finished_at=utc_now(),
            error=f"enqueue failed: {exc}",
            summary=f"enqueue failed: {exc}",
        )
        raise HTTPException(status_code=503, detail=f"failed to enqueue task: {exc}") from exc
    task_store.update(task_id, status="queued", rq_job_id=job_id)
    return TaskCreateResponse(task_id=task_id, status="queued", rq_job_id=job_id)
