"""Run a browser task against the local API and wait for the result.

Examples:
    python -m scripts.run_task demo
    python -m scripts.run_task "打开 https://example.com 提取标题"
    python -m scripts.run_task --json '{"name":"p","steps":[{"action":"goto","url":"https://example.com"}]}'
    python -m scripts.run_task --file plan.json
    python -m scripts.run_task demo --headed --max-replans 1 --webhook-url https://hooks.example/x
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_API = "http://127.0.0.1:8001"
TERMINAL = {"succeeded", "failed", "cancelled"}


def _req(api: str, method: str, path: str, payload: dict | None = None, timeout: int = 30):
    url = api + path
    data = None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"  !! HTTP {exc.code}: {detail}", file=sys.stderr)
        raise SystemExit(1) from exc


def _load_plan(path: str) -> dict:
    raw = Path(path).read_text(encoding="utf-8")
    plan = json.loads(raw)
    if "steps" not in plan:
        raise ValueError("plan file must contain a 'steps' list")
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?", default="demo",
                        help="natural-language task, or 'demo'; ignored with --json/--file")
    parser.add_argument("--api", default=DEFAULT_API, help=f"API base (default {DEFAULT_API})")
    parser.add_argument("--json", dest="plan_json", help="inline JSON plan")
    parser.add_argument("--file", dest="plan_file", help="JSON plan file")
    parser.add_argument("--headed", action="store_true", help="show browser window")
    parser.add_argument("--max-replans", type=int, default=0, help="0..2")
    parser.add_argument("--webhook-url", default="")
    parser.add_argument("--poll", type=float, default=2.0, help="poll interval seconds")
    parser.add_argument("--timeout", type=float, default=120.0, help="max wait seconds")
    args = parser.parse_args()

    if args.plan_json or args.plan_file:
        plan = json.loads(args.plan_json) if args.plan_json else _load_plan(args.plan_file)
        payload = {"json_plan": plan}
        label = f"json_plan ({len(plan.get('steps', []))} steps)"
    elif args.task.strip().lower() == "demo":
        payload = {"demo": True}
        label = "demo"
    else:
        payload = {"task_nl": args.task.strip()}
        label = "task_nl"

    payload.update(
        headed=bool(args.headed),
        max_replans=max(0, min(int(args.max_replans), 2)),
    )
    if args.webhook_url.strip():
        payload["webhook_url"] = args.webhook_url.strip()

    print(f"[run] {label} -> {args.api}")
    st, created = _req(args.api, "POST", "/api/v1/tasks", payload)
    task_id = created["task_id"]
    print(f"[run] created task_id={task_id} status={created.get('status')}")

    t = None
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        time.sleep(args.poll)
        _, t = _req(args.api, "GET", f"/api/v1/tasks/{task_id}")
        if t["status"] in TERMINAL:
            break
    if t is None or t["status"] not in TERMINAL:
        print(f"[run] timeout after {args.timeout:.0f}s; last status={t and t['status']}", file=sys.stderr)
        return 2

    print(f"[run] final status={t['status']} run_id={t.get('run_id') or '-'}")
    if t.get("summary"):
        print(f"[run] summary: {t['summary']}")
    if t.get("error"):
        print(f"[run] error: {t['error']}")
    if t.get("run_id"):
        print(f"[run] report: {t['run_id']}/report.md")
        _, shots = _req(args.api, "GET", f"/api/v1/tasks/{task_id}/screenshots")
        for s in shots:
            print(f"[run] screenshot: {s['path']}")
    return 0 if t["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
