"""Auth / multi-tenant tests: hashing, JWT, principal resolution, RBAC,
owner isolation (file stores), and a Postgres-backed integration suite
(skipped when the container is unreachable)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.security import (
    Principal,
    create_access_token,
    decode_access_token,
    generate_api_key,
    hash_password,
    looks_like_jwt,
    verify_password,
)
from browser_agent.archive import utc_now
from store.artifact_store import reset_artifact_store_cache
from store.factory import reset_task_store_cache, reset_template_store_cache
from store.task_store import TaskRecord, TaskStore
from store.template_store import TemplateRecord, TemplateStore

JWT_SECRET = "test-secret-0123456789-abcdef-0123456789abcdef"


def _fake_settings(tmp_path: Path, *, api_keys: str = "", database_url: str = "") -> SimpleNamespace:
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
        database_url=database_url,
        artifact_store="local",
        s3_endpoint="",
        s3_access_key="",
        s3_secret_key="",
        s3_bucket="",
        s3_region="us-east-1",
        api_keys=api_keys,
        jwt_secret=JWT_SECRET,
        jwt_expires_minutes=60,
    )


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _fake_settings(tmp_path)

    monkeypatch.setattr("api.main.get_settings", lambda: settings)
    monkeypatch.setattr("api.routes_tasks.get_settings", lambda: settings)
    monkeypatch.setattr("api.auth.get_settings", lambda: settings)
    monkeypatch.setattr("api.security.get_settings", lambda: settings)
    monkeypatch.setattr("store.factory.get_settings", lambda: settings)
    monkeypatch.setattr("store.artifact_store.get_settings", lambda: settings)
    monkeypatch.setattr("store.template_store.get_settings", lambda: settings)
    monkeypatch.setattr("store.auth_store.get_settings", lambda: settings)
    reset_task_store_cache()
    reset_artifact_store_cache()
    reset_template_store_cache()
    from store.auth_store import reset_auth_store_cache

    reset_auth_store_cache()
    monkeypatch.setattr("api.main.ping_redis", lambda: True)
    monkeypatch.setattr(
        "api.routes_tasks.enqueue_task",
        lambda task_id: f"job-{task_id}",
    )
    monkeypatch.setattr("api.routes_tasks.cancel_queued_job", lambda _job_id: True)

    with TestClient(app) as c:
        yield c, settings


# ---- password hashing ----


def test_password_hash_roundtrip():
    encoded = hash_password("hunter2!!")
    assert encoded.startswith("pbkdf2$sha256$")
    assert verify_password("hunter2!!", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("hunter2!!", "garbage")


# ---- JWT ----


def test_jwt_roundtrip():
    token, ttl = create_access_token(
        user_id="u-1", username="alice", role="admin", expires_minutes=60
    )
    assert ttl == 3600
    assert looks_like_jwt(token)
    payload = decode_access_token(token)
    assert payload["sub"] == "u-1"
    assert payload["username"] == "alice"
    assert payload["role"] == "admin"
    assert payload["type"] == "access"


def test_jwt_rejects_tampered():
    token, _ = create_access_token(user_id="u-1", username="alice", role="user")
    forged = token[:-4] + ("AAAA" if token[-4:] != "AAAA" else "BBBB")
    with pytest.raises(Exception):
        decode_access_token(forged)


def test_api_key_generation():
    plain, digest = generate_api_key()
    assert plain.startswith("bak_")
    assert len(plain) > 30
    import hashlib

    assert hashlib.sha256(plain.encode()).hexdigest() == digest


# ---- principal resolution via API ----


def test_open_mode_anonymous_allowed(client):
    c, _ = client
    r = c.get("/api/v1/auth/me")
    assert r.status_code == 200
    assert r.json()["auth_type"] == "static_key"
    assert r.json()["is_admin"] is True


def test_static_api_key_required(client):
    c, settings = client
    settings.api_keys = "dev-secret"
    assert c.get("/api/v1/auth/me").status_code == 401
    ok = c.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer dev-secret"}
    )
    assert ok.status_code == 200
    assert ok.json()["principal_id"] == "static"
    bad = c.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer nope"}
    )
    assert bad.status_code == 401


def test_jwt_principal(client):
    c, _ = client
    token, _ = create_access_token(
        user_id="u-alice", username="alice", role="user"
    )
    r = c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["principal_id"] == "u-alice"
    assert body["username"] == "alice"
    assert body["role"] == "user"
    assert body["auth_type"] == "jwt"
    assert body["is_admin"] is False


def _token(user_id: str, username: str, role: str = "user") -> str:
    token, _ = create_access_token(user_id=user_id, username=username, role=role)
    return token


# ---- RBAC ----


def test_read_only_cannot_write(client):
    c, settings = client
    ro = _token("u-ro", "reader", role="read_only")
    headers = {"Authorization": f"Bearer {ro}"}
    r = c.post("/api/v1/tasks", json={"demo": True}, headers=headers)
    assert r.status_code == 403
    r2 = c.post(
        "/api/v1/templates",
        json={"name": "t1", "steps": [{"action": "goto", "url": "https://example.com"}]},
        headers=headers,
    )
    assert r2.status_code == 403


def test_non_admin_cannot_manage_users(client):
    c, _ = client
    token = _token("u-x", "x", role="user")
    r = c.post(
        "/api/v1/auth/users",
        json={"username": "y", "password": "password123"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


# ---- owner isolation (tasks) ----


def test_task_owner_isolation(client):
    c, settings = client
    alice = _token("u-alice", "alice")
    bob = _token("u-bob", "bob")
    admin = _token("u-root", "root", role="admin")

    h_a = {"Authorization": f"Bearer {alice}"}
    h_b = {"Authorization": f"Bearer {bob}"}
    h_admin = {"Authorization": f"Bearer {admin}"}

    created = c.post("/api/v1/tasks", json={"demo": True}, headers=h_a).json()
    task_id = created["task_id"]

    # alice sees her task
    assert c.get("/api/v1/tasks", headers=h_a).json()[0]["task_id"] == task_id
    # bob does not
    assert c.get("/api/v1/tasks", headers=h_b).json() == []
    assert c.get(f"/api/v1/tasks/{task_id}", headers=h_b).status_code == 404
    # admin sees everything
    listed = c.get("/api/v1/tasks", headers=h_admin).json()
    assert any(t["task_id"] == task_id for t in listed)
    # bob cannot cancel alice's task
    assert (
        c.post(f"/api/v1/tasks/{task_id}/cancel", headers=h_b).status_code == 404
    )

    # ownership recorded
    rec = TaskStore(settings.tasks_dir).get(task_id)
    assert rec is not None
    assert rec.owner_id == "u-alice"


def test_static_admin_sees_all(client):
    c, settings = client
    alice = _token("u-alice", "alice")
    created = c.post(
        "/api/v1/tasks", json={"demo": True}, headers={"Authorization": f"Bearer {alice}"}
    ).json()
    listed = c.get("/api/v1/tasks").json()  # anonymous -> static admin in open mode
    assert any(t["task_id"] == created["task_id"] for t in listed)


# ---- owner isolation (templates) ----


def _tpl(name: str) -> dict:
    return {
        "name": name,
        "description": "t",
        "match": name,
        "steps": [{"action": "goto", "url": "https://example.com"}],
    }


def test_template_owner_isolation_and_global_readonly(client):
    c, settings = client
    alice = _token("u-alice", "alice")
    bob = _token("u-bob", "bob")
    h_a = {"Authorization": f"Bearer {alice}"}
    h_b = {"Authorization": f"Bearer {bob}"}

    # alice creates a private template
    r = c.post("/api/v1/templates", json=_tpl("alice-tpl"), headers=h_a)
    assert r.status_code == 201

    # alice sees it, bob does not
    assert any(t["name"] == "alice-tpl" for t in c.get("/api/v1/templates", headers=h_a).json())
    assert c.get("/api/v1/templates", headers=h_b).json() == []
    assert c.get("/api/v1/templates/alice-tpl", headers=h_b).status_code == 404

    # anonymous/static admin creates a global template (owner None)
    c.post("/api/v1/templates", json=_tpl("global-tpl"))
    assert any(t["name"] == "global-tpl" for t in c.get("/api/v1/templates", headers=h_b).json())
    # global template is read-only for bob
    assert (
        c.put(
            "/api/v1/templates/global-tpl",
            json={"description": "hijack"},
            headers=h_b,
        ).status_code
        == 403
    )
    assert (
        c.delete("/api/v1/templates/global-tpl", headers=h_b).status_code == 403
    )
    # but admin can update it
    assert (
        c.put(
            "/api/v1/templates/global-tpl",
            json={"description": "fixed"},
        ).status_code
        == 200
    )


# ---- Postgres-backed integration (skipped without container) ----


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
def test_auth_store_end_to_end():
    db_name = "browser_agent_auth_test"
    url = "postgresql+psycopg://browser:browser@127.0.0.1:5432/" + db_name
    _drop_database_if_exists(db_name)
    _create_database(db_name)
    try:
        from store.auth_store import AuthStore

        store = AuthStore(url)
        admin = store.create_user(
            username="root", password="adminpass1", role="admin"
        )
        store.create_user(username="alice", password="alicepass1", role="user")

        # login succeeds with right password
        row = store.authenticate("alice", "alicepass1")
        assert row.role == "user"
        with pytest.raises(Exception):
            store.authenticate("alice", "wrong")

        # api key: issue, validate, revoke
        data, plain = store.create_api_key(owner_id=admin.user_id, name="ci")
        assert data["key_prefix"] == plain[:8]
        ident = store.authenticate_key(plain)
        assert ident is not None
        assert ident["owner_id"] == admin.user_id
        assert ident["role"] == "admin"

        # revoked key rejected
        assert store.revoke_api_key(data["key_id"]) is True
        assert store.authenticate_key(plain) is None

        # user role default applies
        data2, plain2 = store.create_api_key(owner_id=row.user_id, name="alice-key")
        ident2 = store.authenticate_key(plain2)
        assert ident2 is not None
        assert ident2["role"] == "user"
        assert ident2["username"] == "alice"
    finally:
        _drop_database_if_exists(db_name)
