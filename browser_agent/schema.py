"""Task plan schema: goto / click / type / wait / screenshot / extract."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ALLOWED_ACTIONS = frozenset(
    {"goto", "click", "type", "wait", "screenshot", "extract", "new_tab", "switch_tab", "close_tab"}
)

EXTRACT_FIELDS = frozenset({"title", "url", "text", "html"})


@dataclass
class Step:
    action: str
    # Common optional fields — unused ones stay None / empty
    url: str | None = None
    selector: str | None = None
    text: str | None = None
    ms: int | None = None
    name: str | None = None
    field: str | None = None  # for extract: title|url|text|html
    full_page: bool = False
    note: str = ""
    tab_index: int | None = None  # for switch_tab / close_tab
    new_tab: bool = False  # for goto: open in a new tab

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # drop None for cleaner JSON
        return {k: v for k, v in d.items() if v is not None and v != "" and v is not False}


@dataclass
class Plan:
    name: str
    steps: list[Step] = field(default_factory=list)
    description: str = ""
    source: str = "manual"  # manual | demo | llm | file

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "steps": [s.to_dict() for s in self.steps],
        }


class PlanValidationError(ValueError):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise PlanValidationError(msg)


def validate_step(raw: dict[str, Any], index: int) -> Step:
    _require(isinstance(raw, dict), f"step[{index}] must be an object")
    action = str(raw.get("action", "")).strip().lower()
    _require(action in ALLOWED_ACTIONS, f"step[{index}]: unknown action {action!r}")

    url = raw.get("url")
    selector = raw.get("selector")
    text = raw.get("text")
    ms = raw.get("ms")
    name = raw.get("name")
    field = raw.get("field")
    full_page = bool(raw.get("full_page", False))
    note = str(raw.get("note") or "")
    new_tab = bool(raw.get("new_tab", False))
    tab_index = raw.get("tab_index")

    if action == "goto":
        _require(isinstance(url, str) and url.strip(), f"step[{index}] goto requires url")
        parsed = urlparse(url.strip())
        _require(
            parsed.scheme in ("http", "https") and bool(parsed.netloc),
            f"step[{index}] goto url must be http(s) with host",
        )
        url = url.strip()
    elif action == "click":
        _require(
            isinstance(selector, str) and selector.strip(),
            f"step[{index}] click requires selector",
        )
        selector = selector.strip()
    elif action == "type":
        _require(
            isinstance(selector, str) and selector.strip(),
            f"step[{index}] type requires selector",
        )
        _require(isinstance(text, str), f"step[{index}] type requires text string")
        selector = selector.strip()
        text = text
    elif action == "wait":
        if ms is None:
            ms = 1000
        _require(isinstance(ms, int) and not isinstance(ms, bool), f"step[{index}] wait.ms must be int")
        _require(0 <= int(ms) <= 60_000, f"step[{index}] wait.ms must be 0..60000")
        ms = int(ms)
    elif action == "screenshot":
        if name is None:
            name = f"step-{index + 1}"
        _require(isinstance(name, str) and name.strip(), f"step[{index}] screenshot name invalid")
        # keep filenames simple
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name.strip())
        _require(bool(safe), f"step[{index}] screenshot name empty after sanitize")
        name = safe
    elif action == "extract":
        if field is None:
            field = "title"
        field = str(field).strip().lower()
        _require(field in EXTRACT_FIELDS, f"step[{index}] extract.field must be one of {sorted(EXTRACT_FIELDS)}")
        if field == "text" and selector is not None:
            selector = str(selector).strip() or None
    elif action == "new_tab":
        if url is None:
            url = "about:blank"
        url = str(url).strip() or "about:blank"
    elif action in ("switch_tab", "close_tab"):
        _require(
            isinstance(tab_index, int) and not isinstance(tab_index, bool) and tab_index >= 0,
            f"step[{index}] {action}.tab_index must be a non-negative int",
        )
        tab_index = int(tab_index)

    return Step(
        action=action,
        url=url if isinstance(url, str) else None,
        selector=selector if isinstance(selector, str) else None,
        text=text if isinstance(text, str) else None,
        ms=ms if isinstance(ms, int) else None,
        name=name if isinstance(name, str) else None,
        field=field if isinstance(field, str) else None,
        full_page=full_page,
        note=note,
        tab_index=tab_index if isinstance(tab_index, int) else None,
        new_tab=new_tab,
    )


def parse_plan(data: dict[str, Any] | list[Any], *, source: str = "manual") -> Plan:
    if isinstance(data, list):
        data = {"name": "untitled", "steps": data}
    _require(isinstance(data, dict), "plan must be a JSON object")
    name = str(data.get("name") or "untitled").strip() or "untitled"
    description = str(data.get("description") or "")
    steps_raw = data.get("steps")
    _require(isinstance(steps_raw, list) and len(steps_raw) > 0, "plan.steps must be a non-empty list")
    _require(len(steps_raw) <= 40, "plan.steps exceeds max length 40")
    steps = [validate_step(s if isinstance(s, dict) else {}, i) for i, s in enumerate(steps_raw)]
    return Plan(name=name, steps=steps, description=description, source=source)


def load_plan_file(path: str | Path) -> Plan:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    data = json.loads(text)
    return parse_plan(data, source="file")


def dump_plan(plan: Plan, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
