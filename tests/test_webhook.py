"""Webhook: signature, payload, delivery, retry, and task-terminal trigger."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.main import app
from browser_agent.archive import utc_now
from store.task_store import TaskRecord, TaskStore
from worker.jobs import execute_task
from worker.webhook import (
    build_event_payload,
    notify_task_terminal,
    send_webhook,
    sign_payload,
)

# RFC 4231 test vector (HMAC-SHA256, key="Jefe")
RFC4231 = (
    "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
)


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
        webhook_url="",
        webhook_secret="",
    )


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    s = _fake_settings(tmp_path)
    monkeypatch.setattr("worker.webhook.get_settings", lambda: s)
    monkeypatch.setattr("worker.jobs.get_settings", lambda: s)
    monkeypatch.setattr("store.factory.get_settings", lambda: s)
    from store.factory import reset_task_store_cache

    reset_task_store_cache()
    return s


@pytest.fixture()
def store(settings: SimpleNamespace) -> TaskStore:
    return TaskStore(settings.tasks_dir)


def _rec(**overrides) -> TaskRecord:
    base = dict(
        task_id="wh-1",
        status="succeeded",
        source="demo",
        created_at=utc_now(),
        updated_at=utc_now(),
        finished_at=utc_now(),
        summary="ok",
    )
    base.update(overrides)
    return TaskRecord(**base)


# ---- signature & payload ----


def test_sign_payload_rfc4231():
    assert sign_payload("Jefe", b"what do ya want for nothing?") == RFC4231
    assert sign_payload("", b"data") == ""


def test_build_event_payload_only_terminal():
    assert build_event_payload(_rec(status="running")) is None
    assert build_event_payload(_rec(status="queued")) is None

    p = build_event_payload(_rec())
    assert p is not None
    assert p["event"] == "task.succeeded"
    assert p["task_id"] == "wh-1"
    assert p["status"] == "succeeded"

    assert build_event_payload(_rec(status="failed"))["event"] == "task.failed"
    assert build_event_payload(_rec(status="cancelled"))["event"] == "task.cancelled"


# ---- delivery & retry ----


def test_send_webhook_retries_then_reports_failure(monkeypatch):
    calls = {"n": 0}

    def boom(url, payload, secret, timeout):
        calls["n"] += 1
        raise ConnectionError("refused")

    monkeypatch.setattr("worker.webhook._post_once", boom)
    ok, detail = send_webhook("https://x/h", {"event": "task.failed"}, retries=2)
    assert ok is False
    assert "ConnectionError" in detail
    assert calls["n"] == 3


def test_send_webhook_success(monkeypatch):
    captured = {}

    def ok(url, payload, secret, timeout):
        captured.update(url=url, payload=payload, secret=secret)

    monkeypatch.setattr("worker.webhook._post_once", ok)
    ok_flag, detail = send_webhook(
        "https://x/h", {"event": "task.failed"}, secret="s3cr3t"
    )
    assert ok_flag is True
    assert detail == ""
    assert captured["url"] == "https://x/h"
    assert captured["secret"] == "s3cr3t"


# ---- notify_task_terminal ----


def test_notify_skips_without_url(settings):
    out = notify_task_terminal(_rec())
    assert out["sent"] is False
    assert out["reason"] == ""  # not configured = normal skip, not an error


def test_per_task_url_wins_over_global(settings, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        "worker.webhook.send_webhook",
        lambda url, payload, secret="", retries=2, timeout=10.0: (
            seen.append(url),
            (True, ""),
        )[1],
    )
    settings.webhook_url = "https://global.example/h"
    rec = _rec(webhook_url="https://task.example/h")
    out = notify_task_terminal(rec)
    assert out["sent"] is True
    assert seen == ["https://task.example/h"]


def test_multiple_global_urls(settings, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        "worker.webhook.send_webhook",
        lambda url, payload, secret="", retries=2, timeout=10.0: (
            seen.append(url),
            (True, ""),
        )[1],
    )
    settings.webhook_url = "https://a.example/h, https://b.example/h"
    out = notify_task_terminal(_rec())
    assert out["sent"] is True
    assert seen == ["https://a.example/h", "https://b.example/h"]


def test_send_failure_recorded_on_task(settings, store, monkeypatch):
    monkeypatch.setattr(
        "worker.webhook.send_webhook",
        lambda url, payload, secret="", retries=2, timeout=10.0: (False, "boom"),
    )
    settings.webhook_url = "https://x/h"
    store.create(_rec())
    out = notify_task_terminal(store.get("wh-1"))
    assert out["sent"] is False
    rec = store.get("wh-1")
    assert "webhook failed" not in (rec.error or "")  # notify itself doesn't write

    # jobs._notify_terminal is what records the failure
    from worker.jobs import _notify_terminal

    _notify_terminal(store, "wh-1")
    rec2 = store.get("wh-1")
    assert rec2.error and "webhook failed" in rec2.error and "boom" in rec2.error


# ---- execute_task integration ----


def test_execute_task_fires_webhook_on_terminal(settings, store, monkeypatch):
    from browser_agent.service import RunResult

    fake_result = RunResult(
        run_id="run-wh",
        run_dir=settings.runs_root / "run-wh",
        success=True,
        cancelled=False,
        summary="ok",
        plan={"name": "p", "steps": []},
        report_path=None,
        screenshot_paths=[],
        run_ids=["run-wh"],
    )
    monkeypatch.setattr("worker.jobs.run_plan", lambda req: fake_result)
    notified: list[str] = []
    monkeypatch.setattr(
        "worker.webhook.notify_task_terminal",
        lambda rec: notified.append(rec.status) or {"sent": True},
    )
    store.create(_rec(status="queued", task_id="wh-run", created_at="", updated_at=""))

    result = execute_task("wh-run")
    assert result["status"] == "succeeded"
    assert notified == ["succeeded"]
    assert store.get("wh-run").status == "succeeded"


def test_execute_task_failure_still_fires_webhook(settings, store, monkeypatch):
    def boom(req):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("worker.jobs.run_plan", boom)
    notified: list[str] = []
    monkeypatch.setattr(
        "worker.webhook.notify_task_terminal",
        lambda rec: notified.append(rec.status) or {"sent": True},
    )
    store.create(_rec(status="queued", task_id="wh-fail", created_at="", updated_at=""))

    result = execute_task("wh-fail")
    assert result["status"] == "failed"
    assert notified == ["failed"]
