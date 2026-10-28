"""Template store + CRUD API + planner routing tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.main import app
from browser_agent.planner import plan_from_task
from store.artifact_store import reset_artifact_store_cache
from store.factory import reset_task_store_cache, reset_template_store_cache
from store.template_store import TemplateRecord, TemplateStore


def _fake_settings(tmp_path: Path) -> SimpleNamespace:
    tasks_dir = tmp_path / "tasks"
    runs_root = tmp_path / "runs"
    templates_dir = tmp_path / "templates"
    tasks_dir.mkdir(exist_ok=True)
    runs_root.mkdir(exist_ok=True)
    templates_dir.mkdir(exist_ok=True)
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
        templates_dir=templates_dir,
        task_store="file",
        template_store="file",
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
    monkeypatch.setattr("store.factory.get_settings", lambda: settings)
    monkeypatch.setattr("store.artifact_store.get_settings", lambda: settings)
    monkeypatch.setattr("store.template_store.get_settings", lambda: settings)
    reset_task_store_cache()
    reset_artifact_store_cache()
    reset_template_store_cache()
    monkeypatch.setattr("api.main.ping_redis", lambda: True)
    monkeypatch.setattr(
        "api.routes_tasks.enqueue_task",
        lambda task_id: f"job-{task_id}",
    )
    monkeypatch.setattr("api.routes_tasks.cancel_queued_job", lambda _job_id: True)

    with TestClient(app) as c:
        yield c, settings


SAMPLE_STEPS = [
    {"action": "goto", "url": "https://httpbin.org/html"},
    {"action": "wait", "ms": 500},
    {"action": "screenshot", "name": "page"},
    {"action": "extract", "field": "title"},
]


# ---- store ----


def test_template_store_crud(tmp_path):
    store = TemplateStore(tmp_path / "templates")
    rec = TemplateRecord(
        name="my-demo",
        description="demo",
        match="demo, 演示",
        steps=SAMPLE_STEPS,
    )
    store.create(rec)
    assert store.get("my-demo") is not None
    assert len(store.list_templates()) == 1

    store.update("my-demo", match="new keyword")
    assert store.get("my-demo").match == "new keyword"

    assert store.delete("my-demo") is True
    assert store.get("my-demo") is None
    assert store.delete("my-demo") is False


def test_template_store_name_guard(tmp_path):
    store = TemplateStore(tmp_path / "templates")
    with pytest.raises(ValueError):
        store.create(TemplateRecord(name="../evil", steps=SAMPLE_STEPS))
    with pytest.raises(ValueError):
        store.create(TemplateRecord(name="Bad Name", steps=SAMPLE_STEPS))


# ---- API ----


def test_template_crud_api(client):
    c, _ = client

    r = c.post(
        "/api/v1/templates",
        json={"name": "my-demo", "description": "demo", "match": "demo", "steps": SAMPLE_STEPS},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "my-demo"
    assert body["step_count"] == 4

    listed = c.get("/api/v1/templates")
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "my-demo"

    got = c.get("/api/v1/templates/my-demo")
    assert got.status_code == 200
    assert got.json()["steps"][0]["action"] == "goto"

    r2 = c.put(
        "/api/v1/templates/my-demo",
        json={"description": "updated", "match": "demo, smoke"},
    )
    assert r2.status_code == 200
    assert r2.json()["match"] == "demo, smoke"

    r3 = c.delete("/api/v1/templates/my-demo")
    assert r3.status_code == 204
    assert c.get("/api/v1/templates/my-demo").status_code == 404


def test_template_create_invalid_steps_rejected(client):
    c, _ = client
    r = c.post(
        "/api/v1/templates",
        json={"name": "bad", "steps": [{"action": "explode"}]},
    )
    assert r.status_code == 422

    r2 = c.post(
        "/api/v1/templates",
        json={"name": "bad2", "steps": [{"action": "goto", "url": "ftp://nope"}]},
    )
    assert r2.status_code == 422


def test_template_create_duplicate_conflict(client):
    c, _ = client
    payload = {"name": "dup", "steps": SAMPLE_STEPS}
    assert c.post("/api/v1/templates", json=payload).status_code == 201
    assert c.post("/api/v1/templates", json=payload).status_code == 409


def test_template_invalid_name_rejected(client):
    c, _ = client
    r = c.post(
        "/api/v1/templates",
        json={"name": "../evil", "steps": SAMPLE_STEPS},
    )
    assert r.status_code == 422


# ---- planner routing ----


def test_planner_hits_custom_template(client):
    c, _ = client
    c.post(
        "/api/v1/templates",
        json={
            "name": "weather-check",
            "description": "open httpbin and screenshot",
            "match": "weather, 天气",
            "steps": SAMPLE_STEPS,
        },
    )
    plan = plan_from_task("check the weather please", use_llm=False)
    assert plan.name == "weather-check"
    assert plan.source == "template"
    assert plan.steps[0].action == "goto"

    # match is case-insensitive substring
    plan2 = plan_from_task("Weather tomorrow?", use_llm=False)
    assert plan2.name == "weather-check"


def test_planner_builtin_rule_wins_over_custom(client):
    c, _ = client
    c.post(
        "/api/v1/templates",
        json={"name": "httpbin-tpl", "match": "httpbin", "steps": SAMPLE_STEPS},
    )
    plan = plan_from_task("open httpbin please", use_llm=False)
    # built-in httpbin rule takes priority
    assert plan.name == "httpbin-html"


def test_planner_missing_template_falls_through(client):
    c, _ = client
    with pytest.raises(Exception):
        plan_from_task("do something completely unknown", use_llm=False)
