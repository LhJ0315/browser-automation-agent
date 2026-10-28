"""Planner: demo/template maps + natural language → strict JSON Plan."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .schema import Plan, PlanValidationError, Step, parse_plan


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if not key:
            continue
        if key not in os.environ or not os.environ.get(key):
            os.environ[key] = val


_load_dotenv()


def demo_plan() -> Plan:
    """Deterministic portfolio demo — no LLM, allowlisted host."""
    return Plan(
        name="demo-example-com",
        description="Open example.com → multi-step screenshots → extract title/h1",
        source="demo",
        steps=[
            Step(action="goto", url="https://example.com/", note="open landing page"),
            Step(action="wait", ms=500, note="settle"),
            Step(action="screenshot", name="home", note="landing screenshot"),
            Step(action="extract", field="title", note="page title"),
            Step(action="extract", field="text", selector="h1", note="main heading"),
            Step(action="extract", field="url", note="current url"),
            Step(action="screenshot", name="home-full", full_page=True, note="full page"),
        ],
    )


def template_plan(task: str) -> Plan | None:
    """
    Stage-1 planner: map common NL phrases to templates without LLM.
    Returns None if no template matches.
    """
    t = task.strip().lower()
    if not t:
        return None

    # explicit demo keywords
    if t in {"demo", "demo task", "run demo"} or "示例" in task or "demo" == t:
        return demo_plan()

    # open <url> / 打开 <url>
    m = re.search(
        r"(?:open|goto|visit|打开|访问)\s+(https?://[^\s]+)",
        task,
        flags=re.IGNORECASE,
    )
    if m:
        url = m.group(1).rstrip(".,)］】\"'")
        host = (urlparse(url).hostname or "site").replace(".", "-")
        return Plan(
            name=f"open-{host}",
            description=task.strip(),
            source="demo",
            steps=[
                Step(action="goto", url=url),
                Step(action="wait", ms=800),
                Step(action="screenshot", name="page"),
                Step(action="extract", field="title"),
                Step(action="extract", field="url"),
            ],
        )

    # example.com shortcuts
    if "example.com" in t and any(k in t for k in ("open", "goto", "visit", "打开", "访问", "截图")):
        return demo_plan()

    # httpbin html
    if "httpbin" in t:
        return Plan(
            name="httpbin-html",
            description=task.strip(),
            source="demo",
            steps=[
                Step(action="goto", url="https://httpbin.org/html"),
                Step(action="wait", ms=500),
                Step(action="screenshot", name="httpbin-html"),
                Step(action="extract", field="title"),
                Step(action="extract", field="text", selector="h1"),
            ],
        )

    # user-defined template library (managed via /api/v1/templates)
    return _match_custom_template(task)


def _match_custom_template(task: str) -> Plan | None:
    """Route a NL task to a persisted custom template by keyword match."""
    try:
        from store.factory import get_template_store

        records = get_template_store().list_templates()
    except Exception:  # noqa: BLE001 — planner must never crash on store errors
        return None
    t = task.strip().lower()
    for rec in records:
        keywords = [k.strip().lower() for k in (rec.match or "").split(",") if k.strip()]
        if not keywords:
            continue
        if any(k in t for k in keywords):
            try:
                return parse_plan(
                    {
                        "name": rec.name,
                        "description": rec.description or task.strip(),
                        "steps": rec.steps or [],
                    },
                    source="template",
                )
            except PlanValidationError:
                continue
    return None


def _doubao_config() -> dict[str, str] | None:
    api_key = (
        os.environ.get("DOUBAO_API_KEY")
        or os.environ.get("ARK_API_KEY")
        or os.environ.get("VOLCENGINE_API_KEY")
    )
    if not api_key:
        return None
    model = (
        os.environ.get("DOUBAO_MODEL")
        or os.environ.get("ARK_MODEL")
        or os.environ.get("DOUBAO_ENDPOINT_ID")
    )
    if not model:
        return None
    base_url = (
        os.environ.get("DOUBAO_BASE_URL")
        or os.environ.get("ARK_BASE_URL")
        or "https://ark.cn-beijing.volces.com/api/v3"
    )
    return {"api_key": api_key, "model": model, "base_url": base_url.rstrip("/")}


def config_status() -> dict[str, Any]:
    cfg = _doubao_config()
    return {
        "doubao_configured": cfg is not None,
        "has_api_key": bool(
            os.environ.get("DOUBAO_API_KEY")
            or os.environ.get("ARK_API_KEY")
            or os.environ.get("VOLCENGINE_API_KEY")
        ),
        "has_model": bool(
            os.environ.get("DOUBAO_MODEL")
            or os.environ.get("ARK_MODEL")
            or os.environ.get("DOUBAO_ENDPOINT_ID")
        ),
        "base_url": (cfg or {}).get("base_url")
        or "https://ark.cn-beijing.volces.com/api/v3",
        "model": (cfg or {}).get("model") or "",
    }


def _plan_prompt(task: str) -> str:
    return f"""You are a browser automation planner.
Convert the user task into a single JSON object (no markdown) with this shape:
{{
  "name": "short-kebab-name",
  "description": "echo of the task",
  "steps": [
    {{"action": "goto", "url": "https://example.com/"}},
    {{"action": "wait", "ms": 500}},
    {{"action": "click", "selector": "css-selector"}},
    {{"action": "type", "selector": "css-selector", "text": "hello"}},
    {{"action": "screenshot", "name": "label", "full_page": false}},
    {{"action": "extract", "field": "title"}}
  ]
}}

Allowed actions only: goto, click, type, wait, screenshot, extract.
Rules:
- Prefer stable public demo sites (example.com, example.org, httpbin.org) unless the task names another host.
- Prefer CSS selectors; keep plans ≤ 12 steps.
- extract.field must be one of: title, url, text, html.
- For text extract, optional selector is allowed.
- Every plan should include at least one screenshot and one extract when sensible.
- goto urls must be http(s).

User task:
{task}
"""


def _call_doubao(prompt: str, cfg: dict[str, str]) -> str:
    try:
        from openai import OpenAI  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("openai package required: pip install openai") from exc

    timeout_s = float(os.environ.get("DOUBAO_TIMEOUT") or os.environ.get("ARK_TIMEOUT") or "120")
    client = OpenAI(
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        timeout=timeout_s,
        max_retries=1,
    )
    resp = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {
                "role": "system",
                "content": "You output browser automation plans as a single JSON object only.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=1500,
    )
    choice = resp.choices[0].message
    content = choice.content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
            else:
                parts.append(str(getattr(part, "text", part) or ""))
        return "".join(parts)
    return content or ""


def _call_doubao_vision(prompt: str, cfg: dict, image_b64: str) -> str:
    """Vision-capable call. Requires a vision model endpoint."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package required") from exc
    timeout_s = float(os.environ.get("DOUBAO_TIMEOUT") or "120")
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"],
                    timeout=timeout_s, max_retries=1)
    resp = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": "You output browser automation decisions as a single JSON object only. Look at the screenshot."},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ]},
        ],
        temperature=0.2,
        max_tokens=1500,
    )
    choice = resp.choices[0].message
    content = choice.content
    if isinstance(content, list):
        return "".join(part.get("text","") if isinstance(part,dict) else str(getattr(part,"text","")) for part in content)
    return content or ""


def _parse_llm_plan(raw: str, *, task: str) -> Plan:
    text = raw.strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise PlanValidationError("LLM returned non-JSON plan")
    data = json.loads(m.group(0))
    if isinstance(data, dict) and not data.get("description"):
        data["description"] = task.strip()
    plan = parse_plan(data, source="llm")
    return plan


def plan_from_task(task: str, *, demo: bool = False, use_llm: bool = True) -> Plan:
    """
    Build a Plan from natural language.
    Order: --demo → template map → LLM (if configured) → error.
    """
    if demo:
        return demo_plan()

    templated = template_plan(task)
    if templated is not None:
        return templated

    if use_llm:
        cfg = _doubao_config()
        if cfg:
            raw = _call_doubao(_plan_prompt(task), cfg)
            return _parse_llm_plan(raw, task=task)

    raise PlanValidationError(
        "No template matched and Doubao/ARK is not configured. "
        "Use --demo, --plan FILE, or set DOUBAO_API_KEY + DOUBAO_MODEL."
    )
