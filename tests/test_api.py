"""API tests with TestClient; enqueue stubbed (no Redis required)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.main import app
from browser_agent.archive import utc_now
from store.task_store import TaskRecord, TaskStore


def _fake_settings(tmp_path: Path) -> SimpleNamespace:
    tasks_dir = tmp_path / "tasks"
    runs_root = tmp_path / "runs"
    tasks_dir.mkdir(exist_ok=True)
    runs_root.mkdir(exist_ok=True)
    return SimpleNamespace(
        redis_url="redis://127.0.0.1:6379/0",
        rq_queue="browser_tasks",
        max_task_seconds=300,
        max_browser_workers=1,
        runs_root=runs_root,
        data_root=tmp_path,
        api_host="127.0.0.1",
        api_port=8000,
        tasks_dir=tasks_dir,
        task_store="file",
        database_url=None,
        artifact_store="local",
        s3_endpoint="",
        s3_access_key="",
        s3_secret_key="",
        s3_bucket="",
        s3_region="us-east-1",
    )


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _fake_settings(tmp_path)

    monkeypatch.setattr("api.main.get_settings", lambda: settings)
    monkeypatch.setattr("api.routes_tasks.get_settings", lambda: settings)
    # P1 store/artifact singletons read settings from their own modules;
    # patch those too and clear the process-wide caches so each test rebuilds
    # against tmp_path.
    monkeypatch.setattr("store.factory.get_settings", lambda: settings)
    monkeypatch.setattr("store.artifact_store.get_settings", lambda: settings)
    from store.factory import reset_task_store_cache
    from store.artifact_store import reset_artifact_store_cache
    reset_task_store_cache()
    reset_artifact_store_cache()
    monkeypatch.setattr("api.main.ping_redis", lambda: True)
    monkeypatch.setattr(
        "api.routes_tasks.enqueue_task",
        lambda task_id: f"job-{task_id}",
    )
    monkeypatch.setattr("api.routes_tasks.cancel_queued_job", lambda _job_id: True)

    with TestClient(app) as c:
        yield c, settings


def test_health(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["redis"] is True
    assert body["queue"] == "browser_tasks"


def test_create_demo_task(client):
    c, settings = client
    r = c.post("/api/v1/tasks", json={"demo": True})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["task_id"]
    assert body["rq_job_id"].startswith("job-")

    stored = TaskStore(settings.tasks_dir).get(body["task_id"])
    assert stored is not None
    assert stored.status == "queued"
    assert stored.source == "demo"


def test_create_requires_exactly_one_source(client):
    c, _ = client
    r = c.post("/api/v1/tasks", json={})
    assert r.status_code == 422

    r = c.post("/api/v1/tasks", json={"demo": True, "task_nl": "open https://example.com"})
    assert r.status_code == 422


def test_list_and_get(client):
    c, _ = client
    created = c.post("/api/v1/tasks", json={"demo": True}).json()
    listed = c.get("/api/v1/tasks").json()
    assert any(t["task_id"] == created["task_id"] for t in listed)

    detail = c.get(f"/api/v1/tasks/{created['task_id']}")
    assert detail.status_code == 200
    assert detail.json()["source"] == "demo"


def test_cancel_queued_task(client):
    c, settings = client
    created = c.post("/api/v1/tasks", json={"demo": True}).json()
    task_id = created["task_id"]
    r = c.post(f"/api/v1/tasks/{task_id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["cancel_requested"] is True

    # idempotent on terminal
    r2 = c.post(f"/api/v1/tasks/{task_id}/cancel")
    assert r2.status_code == 200
    assert r2.json()["status"] == "cancelled"

    rec = TaskStore(settings.tasks_dir).get(task_id)
    assert rec is not None
    assert rec.status == "cancelled"


def test_report_and_screenshots(client):
    c, settings = client
    store = TaskStore(settings.tasks_dir)
    task_id = "report-task"
    run_id = "run-test-1"
    run_dir = settings.runs_root / run_id
    steps = run_dir / "steps"
    steps.mkdir(parents=True)
    (run_dir / "report.md").write_text("# ok\n", encoding="utf-8")
    (run_dir / "meta.json").write_text(
        json.dumps({"step_count": 2, "summary": "ok"}), encoding="utf-8"
    )
    png = steps / "01_home.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    store.create(
        TaskRecord(
            task_id=task_id,
            status="succeeded",
            source="demo",
            run_id=run_id,
            run_ids=[run_id],
            created_at=utc_now(),
            updated_at=utc_now(),
            finished_at=utc_now(),
            summary="ok",
        )
    )

    report = c.get(f"/api/v1/tasks/{task_id}/report")
    assert report.status_code == 200
    assert b"# ok" in report.content

    shots = c.get(f"/api/v1/tasks/{task_id}/screenshots")
    assert shots.status_code == 200
    assert shots.json()[0]["name"] == "01_home.png"

    img = c.get(f"/api/v1/tasks/{task_id}/screenshots/01_home.png")
    assert img.status_code == 200
    assert img.headers["content-type"].startswith("image/png")


def test_screenshot_path_traversal_blocked(client):
    c, settings = client
    store = TaskStore(settings.tasks_dir)
    task_id = "escape-task"
    run_id = "run-escape"
    run_dir = settings.runs_root / run_id
    (run_dir / "steps").mkdir(parents=True)
    store.create(
        TaskRecord(
            task_id=task_id,
            status="succeeded",
            source="demo",
            run_id=run_id,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
    )
    # Reject non-png and names containing path separators (handler-level guards).
    r = c.get(f"/api/v1/tasks/{task_id}/screenshots/not-a-png.txt")
    assert r.status_code == 400
    r2 = c.get(f"/api/v1/tasks/{task_id}/screenshots/foo%5Cbar.png")
    assert r2.status_code == 400


def test_report_not_ready(client):
    c, _ = client
    created = c.post("/api/v1/tasks", json={"demo": True}).json()
    r = c.get(f"/api/v1/tasks/{created['task_id']}/report")
    assert r.status_code == 409
