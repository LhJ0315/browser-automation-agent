"""Run archive: plan.json + steps/*.png + report.md + meta.json."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import Plan


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}"


@dataclass
class StepRecord:
    index: int
    action: str
    ok: bool
    message: str = ""
    error: str | None = None
    screenshot: str | None = None  # relative to run dir
    data: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""


@dataclass
class RunArchive:
    run_dir: Path
    run_id: str
    plan: Plan
    meta: dict[str, Any] = field(default_factory=dict)
    steps: list[StepRecord] = field(default_factory=list)
    success: bool = False
    summary: str = ""

    @property
    def steps_dir(self) -> Path:
        return self.run_dir / "steps"

    def write_plan(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.steps_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "plan.json").write_text(
            json.dumps(self.plan.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def add_step(self, record: StepRecord) -> None:
        self.steps.append(record)
        self._write_meta_partial()

    def _write_meta_partial(self) -> None:
        payload = {
            **self.meta,
            "run_id": self.run_id,
            "success": self.success,
            "summary": self.summary,
            "step_count": len(self.steps),
            "steps": [asdict(s) for s in self.steps],
            "updated_at": utc_now(),
        }
        (self.run_dir / "meta.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def finalize(self, *, success: bool, summary: str) -> Path:
        self.success = success
        self.summary = summary
        self.meta["finished_at"] = utc_now()
        self._write_meta_partial()
        report_path = self.run_dir / "report.md"
        report_path.write_text(render_report_md(self), encoding="utf-8")
        return report_path


def create_archive(
    output_root: str | Path,
    plan: Plan,
    *,
    run_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> RunArchive:
    root = Path(output_root)
    rid = run_id or new_run_id("run")
    run_dir = root / rid
    archive = RunArchive(
        run_dir=run_dir,
        run_id=rid,
        plan=plan,
        meta={
            "started_at": utc_now(),
            "plan_name": plan.name,
            "plan_source": plan.source,
            **(meta or {}),
        },
    )
    archive.write_plan()
    return archive


def render_report_md(archive: RunArchive) -> str:
    lines: list[str] = []
    lines.append("# Browser Automation Report")
    lines.append("")
    lines.append(f"- **Run ID**: `{archive.run_id}`")
    lines.append(f"- **Plan**: `{archive.plan.name}`")
    lines.append(f"- **Source**: `{archive.plan.source}`")
    lines.append(f"- **Success**: `{archive.success}`")
    if archive.plan.description:
        lines.append(f"- **Task**: {archive.plan.description}")
    lines.append(f"- **Started**: {archive.meta.get('started_at', '')}")
    lines.append(f"- **Finished**: {archive.meta.get('finished_at', '')}")
    lines.append(f"- **Summary**: {archive.summary}")
    lines.append("")

    if archive.plan.steps:
        lines.append("## Plan")
        lines.append("")
        for i, step in enumerate(archive.plan.steps, 1):
            detail = step.to_dict()
            action = detail.pop("action", "?")
            extras = ", ".join(f"{k}={v!r}" for k, v in detail.items() if k != "note")
            lines.append(f"{i}. `{action}`" + (f" — {extras}" if extras else ""))
        lines.append("")

    lines.append("## Steps")
    lines.append("")
    for rec in archive.steps:
        status = "OK" if rec.ok else "FAIL"
        lines.append(f"### Step {rec.index + 1}: `{rec.action}` — {status}")
        lines.append("")
        if rec.message:
            lines.append(f"- Message: {rec.message}")
        if rec.error:
            lines.append(f"- Error: `{rec.error}`")
        if rec.screenshot:
            lines.append(f"- Screenshot: `{rec.screenshot}`")
            lines.append("")
            lines.append(f"![step {rec.index + 1}]({rec.screenshot})")
        data = rec.data or {}
        if "value" in data:
            val = data["value"]
            preview = val if not isinstance(val, str) else (val if len(val) < 500 else val[:500] + "…")
            lines.append(f"- Extracted `{data.get('field')}`:")
            lines.append("")
            lines.append("```")
            lines.append(str(preview))
            lines.append("```")
        elif data:
            # compact non-extract data
            interesting = {k: v for k, v in data.items() if k not in {"html"}}
            if interesting:
                lines.append(f"- Data: `{json.dumps(interesting, ensure_ascii=False)[:400]}`")
        lines.append("")

    lines.append("## Archive layout")
    lines.append("")
    lines.append("```text")
    lines.append(f"{archive.run_id}/")
    lines.append("  plan.json")
    lines.append("  meta.json")
    lines.append("  report.md")
    lines.append("  steps/*.png")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)
