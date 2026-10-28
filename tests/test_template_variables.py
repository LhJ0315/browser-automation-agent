"""Template variables + Postgres template store + run endpoint tests."""

from __future__ import annotations

import pytest

from store.template_render import collect_variables, render_template


# ---- render pure functions ----

def test_render_replaces_placeholders():
    steps = [
        {"action": "goto", "url": "https://{{host}}/{{path}}"},
        {"action": "type", "selector": "#q", "text": "search {{query}}"},
    ]
    rendered, missing = render_template(
        steps, {"host": "example.com", "path": "search", "query": "browser"}
    )
    assert missing == []
    assert rendered[0]["url"] == "https://example.com/search"
    assert rendered[1]["text"] == "search browser"


def test_render_uses_template_defaults():
    steps = [{"action": "wait", "note": "poll {{seconds}}s"}]
    rendered, missing = render_template(steps, {}, defaults={"seconds": "5"})
    assert missing == []
    assert rendered[0]["note"] == "poll 5s"


def test_render_run_value_overrides_default():
    steps = [{"action": "wait", "note": "poll {{seconds}}s"}]
    rendered, _ = render_template(
        steps, {"seconds": "9"}, defaults={"seconds": "5"}
    )
    assert rendered[0]["note"] == "poll 9s"


def test_render_reports_missing_variables():
    steps = [
        {"action": "goto", "url": "https://{{host}}"},
        {"action": "type", "selector": "#q", "text": "{{query}}"},
    ]
    rendered, missing = render_template(steps, {"host": "x.com"}, defaults={"other": "1"})
    assert missing == ["query"]
    # Original steps unchanged on failure (copy semantics).
    assert rendered[0]["url"] == "https://{{host}}"


def test_render_walks_nested_structures():
    steps = [{"action": "extract", "field": "text", "note": [{"deep": "{{a}}"}, "{{b}}"]}]
    rendered, missing = render_template(steps, {"a": "1", "b": "2"})
    assert missing == []
    assert rendered[0]["note"] == [{"deep": "1"}, "2"]


def test_collect_variables():
    steps = [{"action": "goto", "url": "https://{{host}}/{{host}}/{{x}}"}]
    assert collect_variables(steps) == {"host", "x"}


def test_render_leaves_unknown_placeholders_when_no_missing():
    # All referenced variables are provided, so an extraneous placeholder is
    # not in scope and stays literal.
    steps = [{"action": "wait", "note": "keep {{literal}}"}]
    rendered, missing = render_template(steps, {"literal": "ok", "extra": "ignored"})
    assert missing == []
    assert rendered[0]["note"] == "keep ok"


# ---- Postgres template store (dedicated DB) ----


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
def test_postgres_template_store_crud_and_visibility(tmp_path, monkeypatch):
    db_name = "browser_agent_tmpl_test"
    url = "postgresql+psycopg://browser:browser@127.0.0.1:5432/" + db_name
    _drop_database_if_exists(db_name)
    _create_database(db_name)
    try:
        from store.postgres_template_store import PostgresTemplateStore
        from store.template_store import TemplateRecord

        store = PostgresTemplateStore(url)

        shared = store.create(
            TemplateRecord(
                name="daily-check",
                description="check site",
                match="check, daily",
                steps=[{"action": "goto", "url": "https://{{host}}"}],
                variables={"host": "example.com"},
                owner_id=None,
            )
        )
        alice = store.create(
            TemplateRecord(
                name="alice-only",
                description="private",
                steps=[{"action": "wait", "ms": 500}],
                variables={},
                owner_id="user-alice",
            )
        )

        # duplicate create raises
        with pytest.raises(FileExistsError):
            store.create(TemplateRecord(name="daily-check", steps=[]))

        # visibility: alice sees own + global; bob sees only global
        names_alice = {r.name for r in store.list_templates(visible_to="user-alice")}
        assert names_alice == {"daily-check", "alice-only"}
        names_bob = {r.name for r in store.list_templates(visible_to="user-bob")}
        assert names_bob == {"daily-check"}
        # admin (None) sees everything
        assert len(store.list_templates()) == 2

        # update variables
        updated = store.update("daily-check", variables={"host": "example.org"})
        assert updated.variables == {"host": "example.org"}
        assert store.get("daily-check").variables == {"host": "example.org"}

        # delete
        assert store.delete("alice-only") is True
        assert store.delete("alice-only") is False
        assert store.get("alice-only") is None
    finally:
        _drop_database_if_exists(db_name)


# ---- API run endpoint (file store; fast) ----


def _client():
    from fastapi.testclient import TestClient

    from api.main import app
    from store.factory import reset_task_store_cache, reset_template_store_cache
    from worker.queue import reset_queue_cache

    reset_queue_cache()
    reset_task_store_cache()
    reset_template_store_cache()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_stores():
    yield
    from store.factory import reset_task_store_cache, reset_template_store_cache
    from worker.queue import reset_queue_cache

    reset_queue_cache()
    reset_task_store_cache()
    reset_template_store_cache()


def test_template_run_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "api.routes_templates.enqueue_task", lambda task_id: f"job-{task_id}"
    )
    client = _client()

    # create a template with placeholders + defaults
    resp = client.post(
        "/api/v1/templates",
        json={
            "name": "search-site",
            "description": "search {{site}} for {{query}}",
            "steps": [
                {"action": "goto", "url": "https://{{site}}"},
                {"action": "type", "selector": "#q", "text": "{{query}}"},
            ],
            "variables": {"query": "default-query"},
        },
    )
    assert resp.status_code == 201, resp.text
    detail = resp.json()
    assert detail["variable_count"] == 1
    assert detail["variables"] == {"query": "default-query"}

    # run without required {{site}} -> 422
    resp = client.post("/api/v1/templates/search-site/run", json={"variables": {}})
    assert resp.status_code == 422
    assert "site" in resp.json()["detail"]

    # run with variables -> task queued, json_plan rendered
    resp = client.post(
        "/api/v1/templates/search-site/run",
        json={"variables": {"site": "example.com"}, "headed": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued"

    from store.factory import get_task_store

    rec = get_task_store().get(body["task_id"])
    assert rec is not None
    assert rec.source == "template"
    assert rec.json_plan["steps"][0]["url"] == "https://example.com"
    assert rec.json_plan["steps"][1]["text"] == "default-query"  # default used

    # listing shows variable_count
    resp = client.get("/api/v1/templates")
    assert resp.status_code == 200
    assert any(t["name"] == "search-site" and t["variable_count"] == 1 for t in resp.json())


def test_template_run_updates_variables_and_webhook(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "api.routes_templates.enqueue_task", lambda task_id: f"job-{task_id}"
    )
    client = _client()
    client.post(
        "/api/v1/templates",
        json={
            "name": "var-update",
            "steps": [{"action": "wait", "note": "{{v}}"}],
            "variables": {"v": "old"},
        },
    )
    # update variables via PUT
    resp = client.put(
        "/api/v1/templates/var-update", json={"variables": {"v": "new-default"}}
    )
    assert resp.status_code == 200
    assert resp.json()["variables"] == {"v": "new-default"}
    # run with override
    resp = client.post(
        "/api/v1/templates/var-update/run",
        json={"variables": {"v": "override"}, "webhook_url": "https://hooks.example/x"},
    )
    assert resp.status_code == 200
    from store.factory import get_task_store

    rec = get_task_store().get(resp.json()["task_id"])
    assert rec.json_plan["steps"][0]["note"] == "override"
    assert rec.webhook_url == "https://hooks.example/x"
