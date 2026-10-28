"""Terminal-event webhook notifications.

Sends an HMAC-SHA256-signed POST to configured URL(s) when a task reaches a
terminal state (succeeded / failed / cancelled).

URL selection: per-task ``webhook_url`` (set at task creation) wins over the
global ``WEBHOOK_URL`` env. Both may hold comma-separated URLs; every URL
receives a copy. With no secret configured, signature headers are omitted
(payload is still JSON).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from settings import get_settings
from store.task_store import TaskRecord

_EVENT_BY_STATUS = {
    "succeeded": "task.succeeded",
    "failed": "task.failed",
    "cancelled": "task.cancelled",
}
_TERMINAL = frozenset(_EVENT_BY_STATUS)


def sign_payload(secret: str, body: bytes) -> str:
    """HMAC-SHA256 hex digest of the raw body (empty secret -> '')."""
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def build_event_payload(rec: TaskRecord) -> dict[str, Any] | None:
    if rec.status not in _TERMINAL:
        return None
    return {
        "event": _EVENT_BY_STATUS[rec.status],
        "task_id": rec.task_id,
        "status": rec.status,
        "source": rec.source,
        "run_id": rec.run_id,
        "run_ids": list(rec.run_ids or []),
        "summary": rec.summary,
        "error": rec.error,
        "created_at": rec.created_at,
        "finished_at": rec.finished_at,
    }


def _split_urls(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [u.strip() for u in raw.split(",") if u.strip()]


def _resolve_urls(rec: TaskRecord) -> list[str]:
    """Per-task URL wins; fall back to global env (comma-separated)."""
    return _split_urls(rec.webhook_url) or _split_urls(get_settings().webhook_url)


def _post_once(url: str, payload: dict[str, Any], secret: str, timeout: float) -> None:
    import httpx

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Event": payload["event"],
        "X-Webhook-Timestamp": str(int(time.time())),
        "User-Agent": "browser-automation-agent/0.3",
    }
    sig = sign_payload(secret, body)
    if sig:
        headers["X-Webhook-Signature"] = f"sha256={sig}"
    resp = httpx.post(url, content=body, headers=headers, timeout=timeout)
    resp.raise_for_status()


def send_webhook(
    url: str,
    payload: dict[str, Any],
    *,
    secret: str = "",
    retries: int = 2,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """POST one webhook with a bounded retry. Returns (ok, detail)."""
    attempts = 1 + max(0, int(retries))
    last_err = "no attempt made"
    for i in range(attempts):
        try:
            _post_once(url, payload, secret, timeout)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            if i < attempts - 1:
                time.sleep(0.5 * (i + 1))
    return False, last_err


def notify_task_terminal(rec: TaskRecord) -> dict[str, Any]:
    """Send terminal-event webhooks for a finished task. Never raises.

    No URL configured -> ``{"sent": False, "reason": ""}`` (normal skip, not
    an error); callers should only record failures when ``reason`` is set.
    """
    urls = _resolve_urls(rec)
    if not urls:
        return {"sent": False, "reason": ""}
    payload = build_event_payload(rec)
    if payload is None:
        return {"sent": False, "reason": f"status {rec.status!r} is not terminal"}
    secret = get_settings().webhook_secret
    results: list[dict[str, Any]] = []
    for url in urls:
        ok, detail = send_webhook(url, payload, secret=secret)
        results.append({"url": url, "ok": ok, "detail": detail})
    return {"sent": all(r["ok"] for r in results), "results": results}
