"""Atomic Playwright actions for the plan DSL."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema import Step


@dataclass
class ActionResult:
    ok: bool
    action: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str | None = None
    error: str | None = None


def run_step(page: Any, step: Step, *, steps_dir: Path, index: int, action_timeout_ms: int) -> ActionResult:
    """Execute one validated step against a Playwright sync page."""
    page.set_default_timeout(action_timeout_ms)
    try:
        if step.action == "goto":
            assert step.url
            resp = page.goto(step.url, wait_until="domcontentloaded")
            status = resp.status if resp else None
            return ActionResult(
                ok=True,
                action="goto",
                message=f"navigated to {step.url}",
                data={"url": page.url, "status": status, "title": page.title()},
            )

        if step.action == "click":
            assert step.selector
            page.locator(step.selector).first.click()
            return ActionResult(
                ok=True,
                action="click",
                message=f"clicked {step.selector}",
                data={"selector": step.selector, "url": page.url},
            )

        if step.action == "type":
            assert step.selector is not None and step.text is not None
            loc = page.locator(step.selector).first
            loc.fill(step.text)
            return ActionResult(
                ok=True,
                action="type",
                message=f"typed into {step.selector}",
                data={"selector": step.selector, "text_len": len(step.text)},
            )

        if step.action == "wait":
            ms = int(step.ms or 1000)
            page.wait_for_timeout(ms)
            return ActionResult(ok=True, action="wait", message=f"waited {ms}ms", data={"ms": ms})

        if step.action == "screenshot":
            name = step.name or f"step-{index + 1}"
            steps_dir.mkdir(parents=True, exist_ok=True)
            path = steps_dir / f"{index + 1:02d}_{name}.png"
            page.screenshot(path=str(path), full_page=bool(step.full_page))
            return ActionResult(
                ok=True,
                action="screenshot",
                message=f"saved {path.name}",
                data={"full_page": bool(step.full_page)},
                screenshot_path=str(path),
            )

        if step.action == "extract":
            field = (step.field or "title").lower()
            if field == "title":
                value: Any = page.title()
            elif field == "url":
                value = page.url
            elif field == "html":
                value = page.content()
                if len(value) > 20_000:
                    value = value[:20_000] + "\n<!-- truncated -->"
            elif field == "text":
                if step.selector:
                    value = page.locator(step.selector).first.inner_text()
                else:
                    value = page.locator("body").inner_text()
                if isinstance(value, str) and len(value) > 8_000:
                    value = value[:8_000] + "\n…(truncated)"
            else:
                return ActionResult(ok=False, action="extract", error=f"unknown field {field}")
            return ActionResult(
                ok=True,
                action="extract",
                message=f"extracted {field}",
                data={"field": field, "value": value},
            )

        return ActionResult(ok=False, action=step.action, error=f"unsupported action {step.action}")
    except Exception as exc:  # noqa: BLE001 — surface to executor
        return ActionResult(ok=False, action=step.action, error=f"{type(exc).__name__}: {exc}")
