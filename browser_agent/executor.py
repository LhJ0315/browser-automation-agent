"""Step-wise Playwright executor with fail-fast and logging."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .actions import ActionResult, run_step
from .archive import RunArchive, StepRecord, utc_now
from .safety import SafetyConfig, assert_plan_safe
from .schema import Plan


def execute_plan(
    plan: Plan,
    archive: RunArchive,
    *,
    safety: SafetyConfig | None = None,
    browser_factory: Any | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> RunArchive:
    """
    Execute plan steps sequentially. On first failure, stop (fail-fast),
    still finalize archive with success=False.

    If cancel_check returns True before a step starts, stop cleanly and
    finalize with summary "cancelled".
    """
    cfg = safety or SafetyConfig.from_env()
    assert_plan_safe(plan, cfg)

    # Lazy import so schema/planner unit tests need no playwright binary
    from playwright.sync_api import sync_playwright

    archive.meta.update(
        {
            "headless": cfg.headless,
            "navigation_timeout_ms": cfg.navigation_timeout_ms,
            "action_timeout_ms": cfg.action_timeout_ms,
            "allow_all_hosts": cfg.allow_all_hosts,
        }
    )
    archive.write_plan()

    failed = False
    cancelled = False
    fail_summary = ""

    with sync_playwright() as p:
        if browser_factory:
            browser = browser_factory(p)
        else:
            launch_kwargs: dict[str, Any] = {"headless": cfg.headless}
            executable = os.environ.get("BROWSER_EXECUTABLE", "").strip()
            if executable:
                launch_kwargs["executable_path"] = executable
            # Container tuning: avoid tiny /dev/shm, skip sandbox/GPU in headless,
            # cut background network chatter so domestic sites load faster.
            launch_kwargs["args"] = [
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--no-first-run",
            ]
            browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(viewport={"width": 1280, "height": 720})
        page = context.new_page()
        page.set_default_navigation_timeout(cfg.navigation_timeout_ms)
        page.set_default_timeout(cfg.action_timeout_ms)

        try:
            for i, step in enumerate(plan.steps):
                if cancel_check and cancel_check():
                    cancelled = True
                    fail_summary = "cancelled"
                    break

                started = utc_now()
                result: ActionResult = run_step(
                    page,
                    step,
                    steps_dir=archive.steps_dir,
                    index=i,
                    action_timeout_ms=cfg.action_timeout_ms,
                )
                finished = utc_now()
                screenshot_rel = None
                if result.screenshot_path:
                    try:
                        screenshot_rel = str(
                            Path(result.screenshot_path).resolve().relative_to(archive.run_dir.resolve())
                        ).replace("\\", "/")
                    except ValueError:
                        screenshot_rel = result.screenshot_path

                rec = StepRecord(
                    index=i,
                    action=step.action,
                    ok=result.ok,
                    message=result.message,
                    error=result.error,
                    screenshot=screenshot_rel,
                    data=result.data,
                    started_at=started,
                    finished_at=finished,
                )
                archive.add_step(rec)

                if not result.ok:
                    failed = True
                    fail_summary = result.error or result.message or "step failed"
                    break
        finally:
            context.close()
            browser.close()

    if cancelled:
        archive.finalize(success=False, summary="cancelled")
    elif failed:
        archive.finalize(success=False, summary=f"Failed: {fail_summary}")
    else:
        extracts = []
        for s in archive.steps:
            if s.action == "extract" and s.data.get("value") is not None:
                extracts.append(f"{s.data.get('field')}={s.data.get('value')!r}")
        summary = "All steps succeeded"
        if extracts:
            summary += "; " + "; ".join(extracts[:5])
        archive.finalize(success=True, summary=summary)

    return archive
