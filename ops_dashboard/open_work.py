"""What is still open, and where it is written down.

Enrique's notes already say it: an unchecked box under a heading that
names the project. The product was indexing the boxes but throwing away
the two things that make them useful — **which section they are in** (his
own classification: "Decisiones pendientes (bloqueantes)", "Próximos
pasos", "Pendiente") and **which project the section belongs to**.

This module puts the two back together:

* an open item inherits the project of the section it lives in;
* it keeps the heading, so the page can link to the exact place in the
  note where Enrique wrote it;
* the sections that say they are blocking say so in the page too.

Nothing here is invented: every line is Enrique's own, in his order.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db

log = logging.getLogger("conductloop.openwork")

# An open checkbox: "- [ ] something". The checked ones are work that is
# already done, which is not what this is about.
_OPEN_BOX = re.compile(r"^\s*[-*]\s+\[\s\]\s+(.+?)\s*$")

# A date in a heading: "estado real: 24-oct, 22:45", "2026-10-23 — ...".
_DATES = (
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})-([a-z]{3})[a-z]*\b", re.IGNORECASE),
)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"])}

# The owner at the end of the line: "- [ ] hacer X — Claude".
_OWNER = re.compile(r"\s*[—–-]\s*(Enrique|Claude|Codex|Cursor|ChatGPT)\s*$")

# Headings that say, in his words, that these ones are blocking.
_BLOCKING = re.compile(
    r"bloqueant|blocking|blocker|open question|pregunta abierta|"
    r"decisi[oó]n pendiente|pending decision|esperando|waiting",
    re.IGNORECASE)


def _section_date(heading: str, year: int) -> str:
    """The date of a heading, as YYYY-MM-DD, when it carries one.

    A heading usually says "24-oct" and not the year. The year is the one
    of the note it lives in — the note was touched that year, so the date
    it carries is of that year.
    """
    match = _DATES[0].search(heading)
    if match:
        return match.group(0)
    match = _DATES[1].search(heading)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            return f"{year:04d}-{month:02d}-{int(match.group(1)):02d}"
    return ""


def _project_of(heading: str) -> str:
    """The project a heading belongs to, if it names one."""
    from . import notes_structure

    text = (heading or "").lower()
    if not text:
        return ""
    for proj in notes_structure.projects.PROJECTS:
        for needle, _name in notes_structure._needles(proj):
            if needle and needle in text:
                return proj.key
    return ""


def _notes_with_open_boxes() -> list[str]:
    """The notes that have something open, so only those are read."""
    try:
        with db.db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT d.path FROM todos t JOIN documents d ON d.id = t.doc_id "
                "WHERE t.done = 0 AND t.source = 'obsidian' AND d.path LIKE '%.md'"
            ).fetchall()
        return [str(r["path"]) for r in rows]
    except Exception as e:
        log.warning("could not find the notes with open work: %s", e)
        return []


def scan(limit_per_project: int = 0) -> dict[str, list[dict[str, Any]]]:
    """The open work of every project.

    Each item knows the note it lives in, the section it is under (the
    heading, so the page can link to the exact place), the project of that
    section, and whether the section says it is blocking.

    ``limit_per_project`` caps the items per project; 0 means all of them.
    """
    from . import notes_structure

    out: dict[str, list[dict[str, Any]]] = {}
    for path in _notes_with_open_boxes():
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        heading = ""
        section_key = ""
        try:
            year = datetime.fromtimestamp(Path(path).stat().st_mtime).year
        except OSError:
            year = datetime.now().year
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip()
                section_key = _project_of(heading)
                continue
            match = _OPEN_BOX.match(line)
            if not match:
                continue
            key = section_key or _project_of(heading)
            if not key:
                continue
            item = match.group(1).strip()
            owner = ""
            found = _OWNER.search(item)
            if found:
                owner = found.group(1)
                item = item[:found.start()].strip()
            out.setdefault(key, []).append({
                "path": path,
                "section": heading,
                "date": _section_date(heading, year),
                "blocking": bool(_BLOCKING.search(heading)),
                "owner": owner,
                "text": item[:300],
            })

    for key, rows in out.items():
        # Newest section first, and inside a section the order he wrote it.
        rows.sort(key=lambda row: (row["date"] or "9999"), reverse=True)
        if limit_per_project:
            out[key] = rows[:limit_per_project]
    _ = notes_structure  # the import keeps the module honest about its dependency
    return out


def counts() -> dict[str, int]:
    """How many things are open, per project."""
    return {key: len(rows) for key, rows in scan().items()}
