"""Skills: the rules the bot trades by, where they came from, and whether they are trusted.

Lifecycle:  draft  ->  approved  ->  disabled
- draft:    extracted from a document or proposed in the teaching chat; NOT used for live decisions
- approved: the owner reviewed it (ideally after a backtest); used by the decision layer
- disabled: kept for the record, ignored

Seed skills ship with the app (``seed/*.md``) and are approved on first run - they are
the baseline every serious trading book agrees on, not anyone's secret sauce.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..db import Database

SEED_DIR = Path(__file__).parent / "seed"
CATEGORIES = ("risk", "entry", "exit", "regime", "psychology", "market")


def parse_seed(md: str) -> list[dict[str, str]]:
    """Seed files are markdown: '## <category>' headings, then '- **Name**: rule' bullets."""
    out: list[dict[str, str]] = []
    cat = "market"
    for line in md.splitlines():
        m = re.match(r"^##\s+(\w+)", line)
        if m and m.group(1).lower() in CATEGORIES:
            cat = m.group(1).lower()
            continue
        m = re.match(r"^-\s+\*\*(.+?)\*\*\s*[:：]\s*(.+)$", line)
        if m:
            out.append({"name": m.group(1).strip(), "category": cat, "rule": m.group(2).strip()})
    return out


def load_seed_skills(db: Database) -> int:
    """Insert the seed skills that are not present yet. Returns how many were added."""
    added = 0
    for f in sorted(SEED_DIR.glob("*.md")):
        for s in parse_seed(f.read_text(encoding="utf-8")):
            if not db.skill_exists(s["name"]):
                db.add_skill(s["name"], s["category"], s["rule"], source=f"seed:{f.stem}", status="approved")
                added += 1
    return added


def add_extracted(db: Database, skills: list[dict[str, Any]], source: str, status: str = "draft") -> int:
    n = 0
    for s in skills:
        if db.skill_exists(s["name"]):
            continue
        cat = s.get("category", "market")
        if cat not in CATEGORIES:
            cat = "market"
        rule = s["rule"]
        if s.get("evidence"):
            rule = f"{rule}\n(source: {s['evidence'][:300]})"
        db.add_skill(s["name"], cat, rule, source=source, status=status)
        n += 1
    return n


def active_skills(db: Database) -> list[dict[str, Any]]:
    return [dict(r) for r in db.skills(status="approved")]


def skills_prompt_block(db: Database, max_chars: int = 12_000) -> list[dict[str, Any]]:
    """Approved skills, highest weight first, trimmed so the prompt stays bounded."""
    rows = sorted(active_skills(db), key=lambda r: -float(r["weight"]))
    out, used = [], 0
    for r in rows:
        line = len(r["name"]) + len(r["rule"]) + 10
        if used + line > max_chars:
            break
        out.append({"name": r["name"], "category": r["category"], "rule": r["rule"]})
        used += line
    return out
