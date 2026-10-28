"""Cross-session episodic memory for the agent.

Stores past successful (goal -> summary, key steps) records in SQLite.
Before a new run, we pull 1-2 similar records by keyword overlap and
prepend them as "past experience" into the system prompt.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import List, Dict

_DB = Path(__file__).resolve().parent.parent / "runs" / "memory.sqlite"


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB)
    c.execute(
        """CREATE TABLE IF NOT EXISTS memories (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               goal TEXT UNIQUE,
               summary TEXT,
               steps TEXT,
               created_at TEXT DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    c.commit()
    return c


def save(goal: str, summary: str, steps: List[Dict]) -> None:
    """Store a successful run. Upsert by goal."""
    try:
        c = _conn()
        c.execute(
            "INSERT OR REPLACE INTO memories (goal, summary, steps) VALUES (?, ?, ?)",
            (goal[:200], summary[:500], json.dumps(steps, ensure_ascii=False)[:4000]),
        )
        c.commit()
        c.close()
    except Exception:
        pass  # memory must never break the run


def _keywords(text: str) -> set:
    # crude Chinese/keyword extraction: split on non-alnum, drop short tokens
    toks = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}", text.lower())
    return set(toks)


def recall(goal: str, limit: int = 2) -> List[Dict]:
    """Return up to `limit` past records most similar to goal."""
    try:
        c = _conn()
        rows = c.execute("SELECT goal, summary FROM memories ORDER BY id DESC LIMIT 50").fetchall()
        c.close()
    except Exception:
        return []
    if not rows:
        return []
    gk = _keywords(goal)
    scored = []
    for g, s in rows:
        score = len(gk & _keywords(g))
        if score > 0:
            scored.append((score, g, s))
    scored.sort(reverse=True)
    return [{"goal": g, "summary": s} for _, g, s in scored[:limit]]


def format_for_prompt(goal: str) -> str:
    hits = recall(goal)
    if not hits:
        return ""
    lines = [""]
    lines.append("Past experience (similar past tasks — reuse what worked, avoid what failed):")
    for i, h in enumerate(hits, 1):
        lines.append(f"  {i}. Past goal: {h['goal']}")
        lines.append(f"     Outcome: {h['summary']}")
    lines.append("")
    return "\n".join(lines)
