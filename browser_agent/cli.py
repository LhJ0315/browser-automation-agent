"""CLI for the browser automation agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .planner import config_status
from .schema import PlanValidationError
from .service import RunRequest, package_root, resolve_plan, run_plan


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="browser-automation-agent",
        description="Natural language / JSON plan → Playwright steps → screenshot archive.",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--demo",
        action="store_true",
        help="Run built-in example.com plan (no API key).",
    )
    src.add_argument(
        "--task",
        type=str,
        default=None,
        help="Natural-language task (template map, else Doubao/ARK LLM).",
    )
    src.add_argument(
        "--plan",
        type=str,
        default=None,
        help="Path to a JSON plan file.",
    )
    p.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output root for runs/ (default: runs/).",
    )
    p.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Optional run directory name under output root.",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="Show browser window (default: headless).",
    )
    p.add_argument(
        "--allow-all-hosts",
        action="store_true",
        help="Disable host allowlist (use with care).",
    )
    p.add_argument(
        "--max-replans",
        type=int,
        default=0,
        help="On failure, ask LLM to replan up to N times (0=off, max 2).",
    )
    p.add_argument(
        "--print-plan",
        action="store_true",
        help="Print resolved plan JSON and exit without running browser.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not (args.demo or args.task or args.plan):
        print("error: provide --demo, --task, or --plan", file=sys.stderr)
        return 2

    try:
        plan = resolve_plan(
            demo=bool(args.demo),
            task=args.task,
            plan_path=args.plan,
        )
    except (PlanValidationError, OSError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        status = config_status()
        print(
            f"hint: doubao_configured={status['doubao_configured']} "
            f"model={status['model'] or '-'}",
            file=sys.stderr,
        )
        return 2

    if args.print_plan:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0

    output_root = Path(args.output) if args.output else (package_root() / "runs")
    result = run_plan(
        RunRequest(
            demo=bool(args.demo),
            task=args.task,
            plan=plan,
            output_root=output_root,
            run_id=args.run_id,
            headed=bool(args.headed),
            allow_all_hosts=bool(args.allow_all_hosts),
            max_replans=int(args.max_replans),
        )
    )

    print(f"[run] {result.run_dir}")
    for rid in result.run_ids:
        if rid != result.run_id:
            print(f"[run-attempt] {output_root / rid}")

    if result.cancelled:
        print(f"[cancelled] {result.summary}", file=sys.stderr)
        if result.report_path:
            print(f"[report] {result.report_path}", file=sys.stderr)
        return 1

    if result.success:
        print(f"[ok] {result.summary}")
        if result.report_path:
            print(f"[report] {result.report_path}")
        return 0

    print(f"[fail] {result.summary}", file=sys.stderr)
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
    if result.report_path:
        print(f"[report] {result.report_path}", file=sys.stderr)
    # validation-style failures → exit 2
    if result.error and "replan failed" not in result.error and "Failed:" not in (result.summary or ""):
        if "not in allowlist" in (result.error or "") or "validation" in (result.error or "").lower():
            return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
