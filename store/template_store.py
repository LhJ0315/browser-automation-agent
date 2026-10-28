"""File-backed plan template store (`data/templates/{name}.json`).

Templates persist reusable plan step lists. The planner consults this
library after built-in NL rules miss, matching on comma-separated keywords.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

from browser_agent.archive import utc_now
from settings import get_settings

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass
class TemplateRecord:
    name: str
    description: str = ""
    # Comma-separated keywords; any substring hit on a NL task routes to this template.
    match: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    # Variable defaults: {"var_name": "default text"}. Steps may contain
    # {{var_name}} placeholders; run-time values override these defaults.
    variables: dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    # Multi-tenant: owning user_id (None = global/public template).
    owner_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TemplateRecord":
        known = {f.name for f in fields(cls)}
        payload = {k: v for k, v in data.items() if k in known}
        return cls(**payload)


def validate_template_name(name: str) -> str:
    clean = (name or "").strip().lower()
    if not _NAME_RE.match(clean):
        raise ValueError(
            "template name must be 1-64 chars: lowercase letters, digits, '-' or '_'"
        )
    return clean


class TemplateStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str) -> Path:
        safe = validate_template_name(name)
        return self.root / f"{safe}.json"

    def list_templates(self, *, visible_to: str | None = None) -> list[TemplateRecord]:
        """``visible_to=None`` -> all; else own + global (owner_id is None)."""
        records: list[TemplateRecord] = []
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                records.append(TemplateRecord.from_dict(data))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        if visible_to is not None:
            records = [
                r
                for r in records
                if r.owner_id is None or r.owner_id == visible_to
            ]
        records.sort(key=lambda r: r.name)
        return records

    def get(self, name: str) -> TemplateRecord | None:
        path = self.path_for(name)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return TemplateRecord.from_dict(data)

    def create(self, record: TemplateRecord) -> TemplateRecord:
        now = utc_now()
        record.name = validate_template_name(record.name)
        record.created_at = record.created_at or now
        record.updated_at = now
        dest = self.path_for(record.name)
        if dest.exists():
            raise FileExistsError(f"template already exists: {record.name}")
        self._write(dest, record.to_dict())
        return record

    def save(self, record: TemplateRecord) -> TemplateRecord:
        record.updated_at = utc_now()
        self._write(self.path_for(record.name), record.to_dict())
        return record

    def update(self, name: str, **fields_value: Any) -> TemplateRecord:
        rec = self.get(name)
        if rec is None:
            raise FileNotFoundError(f"template not found: {name}")
        for key, value in fields_value.items():
            if not hasattr(rec, key):
                raise AttributeError(key)
            setattr(rec, key, value)
        return self.save(rec)

    def delete(self, name: str) -> bool:
        path = self.path_for(name)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def _write(self, dest: Path, payload: dict[str, Any]) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=dest.name, suffix=".tmp", dir=dest.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp_name, dest)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


@lru_cache(maxsize=1)
def get_template_store() -> TemplateStore:
    """Process-wide template store singleton."""
    return TemplateStore(get_settings().templates_dir)


def reset_template_store_cache() -> None:
    """Test helper: clear the cached singleton."""
    get_template_store.cache_clear()
