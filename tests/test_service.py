"""Tests for resolve_plan / run_plan (no real browser)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from browser_agent.schema import PlanValidationError
from browser_agent.service import RunRequest, resolve_plan, run_plan


def test_resolve_demo_plan():
    plan = resolve_plan(demo=True)
    assert plan.name
    assert any(s.action == "goto" for s in plan.steps)


def test_resolve_plan_dict():
    plan = resolve_plan(
        plan={
            "name": "manual",
            "steps": [
                {"action": "goto", "url": "https://example.com/"},
                {"action": "screenshot", "name": "home"},
            ],
        }
    )
    assert plan.name == "manual"
    assert len(plan.steps) == 2


def test_resolve_plan_file():
    path = Path(__file__).resolve().parent.parent / "plans" / "example_com.json"
    plan = resolve_plan(plan_path=path)
    assert plan.name == "example-com-basic"


def test_resolve_rejects_multiple_sources():
    with pytest.raises(PlanValidationError):
        resolve_plan(demo=True, task="open https://example.com")


def test_run_plan_cancel_before_start(tmp_path: Path):
    result = run_plan(
        RunRequest(
            demo=True,
            output_root=tmp_path,
            cancel_check=lambda: True,
        )
    )
    assert result.cancelled is True
    assert result.success is False
    assert result.summary == "cancelled"
    assert result.attempts == 0


def test_run_plan_cancel_between_steps(tmp_path: Path):
    calls = {"n": 0}

    def cancel_after_first() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    class FakePage:
        def __init__(self) -> None:
            self.url = "https://example.com/"

        def set_default_navigation_timeout(self, *_a, **_k):
            return None

        def set_default_timeout(self, *_a, **_k):
            return None

        def goto(self, url, *_a, **_k):
            self.url = url
            return SimpleNamespace(ok=True, status=200)

        def screenshot(self, path=None, **_k):
            if path:
                Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
            return None

        def title(self):
            return "Example Domain"

        def inner_text(self, *_a, **_k):
            return "Example Domain"

        def click(self, *_a, **_k):
            return None

        def fill(self, *_a, **_k):
            return None

        def wait_for_timeout(self, *_a, **_k):
            return None

        def wait_for_selector(self, *_a, **_k):
            return None

        def locator(self, *_a, **_k):
            return self

        def first(self):
            return self

        def count(self):
            return 1

    class FakeContext:
        def new_page(self):
            return FakePage()

        def close(self):
            return None

    class FakeBrowser:
        def new_context(self, **_k):
            return FakeContext()

        def close(self):
            return None

    def factory(_p):
        return FakeBrowser()

    result = run_plan(
        RunRequest(
            demo=True,
            output_root=tmp_path,
            browser_factory=factory,
            cancel_check=cancel_after_first,
        )
    )
    assert result.cancelled is True
    assert result.success is False
    assert "cancelled" in (result.summary or "")
    assert result.attempts >= 1
    assert (result.run_dir / "meta.json").is_file()
