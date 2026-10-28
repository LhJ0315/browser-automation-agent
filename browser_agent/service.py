"""Shared run_plan service used by CLI and RQ worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .archive import create_archive, new_run_id
from .agent_loop import run_agent_loop
from .executor import execute_plan
from .planner import config_status, demo_plan, plan_from_task
from .safety import SafetyConfig
from .schema import Plan, PlanValidationError, load_plan_file, parse_plan


def package_root() -> Path:
    return Path(__file__).resolve().parent.parent


@dataclass
class RunRequest:
    demo: bool = False
    task: str | None = None
    agent: bool = False
    plan: Plan | dict[str, Any] | None = None
    plan_path: str | Path | None = None
    output_root: Path | None = None
    run_id: str | None = None
    headed: bool = False
    allow_all_hosts: bool = False
    max_replans: int = 0
    cancel_check: Callable[[], bool] | None = None
    browser_factory: Any | None = None
    meta_extra: dict[str, Any] | None = None
    variables: dict[str, str] | None = None


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    success: bool
    cancelled: bool
    summary: str
    plan: dict[str, Any]
    report_path: Path | None
    screenshot_paths: list[str] = field(default_factory=list)
    attempts: int = 1
    run_ids: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "success": self.success,
            "cancelled": self.cancelled,
            "summary": self.summary,
            "plan": self.plan,
            "report_path": str(self.report_path) if self.report_path else None,
            "screenshot_paths": list(self.screenshot_paths),
            "attempts": self.attempts,
            "run_ids": list(self.run_ids),
            "error": self.error,
        }


def resolve_plan(
    *,
    demo: bool = False,
    task: str | None = None,
    plan: Plan | dict[str, Any] | None = None,
    plan_path: str | Path | None = None,
) -> Plan:
    sources = sum(
        [
            bool(demo),
            bool(task),
            plan is not None,
            plan_path is not None,
        ]
    )
    if sources != 1:
        raise PlanValidationError("provide exactly one of demo / task / plan / plan_path")

    if demo:
        return demo_plan()
    if plan_path is not None:
        return load_plan_file(plan_path)
    if isinstance(plan, Plan):
        return plan
    if isinstance(plan, dict):
        return parse_plan(plan, source="manual")
    if task:
        return plan_from_task(task, demo=False, use_llm=True)
    raise PlanValidationError("no plan source provided")


def _screenshot_paths(run_dir: Path) -> list[str]:
    steps_dir = run_dir / "steps"
    if not steps_dir.is_dir():
        return []
    paths: list[str] = []
    for path in sorted(steps_dir.glob("*.png")):
        paths.append(path.relative_to(run_dir).as_posix())
    return paths


def run_plan(req: RunRequest) -> RunResult:
    """Resolve plan, execute (with optional replan), return structured result."""
    max_replans = max(0, min(int(req.max_replans), 2))
    safety = SafetyConfig.from_env(
        headless=not bool(req.headed),
        allow_all_hosts=bool(req.allow_all_hosts),
    )

    # --- Closed-loop agent mode: goal-first, observe-decide-act until done ---
    if req.agent:
        goal = (req.task or "").strip()
        if not goal:
            raise PlanValidationError("agent mode requires task (goal)")
        # apply template variables {{var}} -> value
        if req.variables:
            for k, v in req.variables.items():
                goal = goal.replace("{{" + k + "}}", str(v))
        output_root = Path(req.output_root) if req.output_root else (package_root() / "runs")
        base_run_id = req.run_id or new_run_id("run")
        placeholder = Plan(
            name="agent-goal",
            description=goal,
            source="agent",
        )
        meta = {"request": {"agent": True, "task": goal, "headed": bool(req.headed)}}
        if req.meta_extra:
            meta.update(req.meta_extra)
        archive = create_archive(output_root, placeholder, run_id=base_run_id, meta=meta)
        run_agent_loop(
            goal, archive, safety=safety, cancel_check=req.cancel_check,
        )
        report_path = archive.run_dir / "report.md"
        return RunResult(
            run_id=archive.run_id,
            run_dir=archive.run_dir,
            success=bool(archive.success),
            cancelled=archive.summary == "cancelled",
            summary=archive.summary or "",
            plan=archive.plan.to_dict(),
            report_path=report_path if report_path.is_file() else None,
            screenshot_paths=_screenshot_paths(archive.run_dir),
            attempts=1,
            run_ids=[archive.run_id],
            error=None if archive.success else archive.summary,
        )

    plan = resolve_plan(
        demo=req.demo,
        task=req.task,
        plan=req.plan,
        plan_path=req.plan_path,
    )
    output_root = Path(req.output_root) if req.output_root else (package_root() / "runs")
    base_run_id = req.run_id or new_run_id("run")
    safety = SafetyConfig.from_env(
        headless=not bool(req.headed),
        allow_all_hosts=bool(req.allow_all_hosts),
    )

    attempt = 0
    run_ids: list[str] = []
    last_archive = None
    cancelled = False
    error: str | None = None

    while True:
        if req.cancel_check and req.cancel_check():
            cancelled = True
            break

        attempt += 1
        rid = base_run_id if attempt == 1 else f"{base_run_id}-replan{attempt - 1}"
        run_ids.append(rid)
        meta = {
            "attempt": attempt,
            "request": {
                "demo": bool(req.demo),
                "task": req.task,
                "plan_path": str(req.plan_path) if req.plan_path else None,
                "headed": bool(req.headed),
                "allow_all_hosts": bool(req.allow_all_hosts),
                "max_replans": max_replans,
            },
            "llm": config_status(),
        }
        if req.meta_extra:
            meta.update(req.meta_extra)

        archive = create_archive(output_root, plan, run_id=rid, meta=meta)
        last_archive = archive
        try:
            execute_plan(
                plan,
                archive,
                safety=safety,
                browser_factory=req.browser_factory,
                cancel_check=req.cancel_check,
            )
        except PlanValidationError as exc:
            archive.finalize(success=False, summary=str(exc))
            error = str(exc)
            break
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            archive.finalize(success=False, summary=msg)
            error = msg
            break

        if archive.summary == "cancelled":
            cancelled = True
            break

        if archive.success:
            break

        if attempt > max_replans or not req.task:
            break

        replan_task = (
            f"{req.task}\n\nPrevious plan failed: {archive.summary}\n"
            "Produce a revised safer plan using allowlisted public sites if possible."
        )
        try:
            plan = plan_from_task(replan_task, demo=False, use_llm=True)
        except (PlanValidationError, RuntimeError) as exc:
            error = f"replan failed: {exc}"
            break

    if last_archive is None:
        # Cancelled before any archive was created.
        empty_dir = output_root / base_run_id
        return RunResult(
            run_id=base_run_id,
            run_dir=empty_dir,
            success=False,
            cancelled=True,
            summary="cancelled",
            plan=plan.to_dict(),
            report_path=None,
            screenshot_paths=[],
            attempts=0,
            run_ids=[],
            error=None,
        )

    report_path = last_archive.run_dir / "report.md"
    if not report_path.is_file():
        report_path = None

    return RunResult(
        run_id=last_archive.run_id,
        run_dir=last_archive.run_dir,
        success=bool(last_archive.success) and not cancelled,
        cancelled=cancelled,
        summary=last_archive.summary or (error or ""),
        plan=last_archive.plan.to_dict(),
        report_path=report_path,
        screenshot_paths=_screenshot_paths(last_archive.run_dir),
        attempts=attempt,
        run_ids=run_ids,
        error=error,
    )
