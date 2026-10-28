"""Template variable rendering: fill ``{{var}}`` placeholders in template steps.

Template steps are arbitrary JSON (dicts/lists). Every string value is treated
as a template: ``{{name}}`` placeholders are replaced with the merged value of
the run-time ``variables`` (overriding the template's declared defaults).

A placeholder with neither a run-time value nor a template default is reported
as *missing*; callers decide (e.g. HTTP 422) what to do with it.
"""

from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def collect_variables(value: Any, found: set[str] | None = None) -> set[str]:
    """Names of every ``{{name}}`` placeholder found anywhere in ``value``."""
    found = set() if found is None else found
    if isinstance(value, str):
        found.update(_PLACEHOLDER.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            collect_variables(v, found)
    elif isinstance(value, list):
        for v in value:
            collect_variables(v, found)
    return found


def render_value(value: Any, values: dict[str, str]) -> Any:
    """Recursively replace ``{{name}}`` in strings; dicts/lists are walked."""
    if isinstance(value, str):
        def _sub(match: re.Match[str]) -> str:
            return values.get(match.group(1), match.group(0))

        return _PLACEHOLDER.sub(_sub, value)
    if isinstance(value, dict):
        return {k: render_value(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, values) for v in value]
    return value


def render_template(
    steps: list[dict[str, Any]],
    variables: dict[str, str] | None = None,
    *,
    defaults: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Render template steps with merged variables.

    Returns ``(rendered_steps, missing_variables)``. ``missing`` is non-empty
    when a placeholder has neither a run-time value nor a declared default.
    """
    defaults = dict(defaults or {})
    provided = dict(variables or {})
    values = {**defaults, **provided}

    missing: list[str] = []
    for name in sorted(collect_variables(steps)):
        if name not in values:
            missing.append(name)
    if missing:
        # No substitution for missing names; callers handle the 422.
        return [dict(s) for s in steps], missing

    return [dict(render_value(s, values)) for s in steps], []
