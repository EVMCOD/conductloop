"""What is still open, and where it is written down.

Your notes already say it: an unchecked box under a heading that names the
project. The product was indexing the boxes but throwing away the two things
that make them useful — **which section they are in** (your own
classification: "Blocking decisions", "Next steps", "Pending") and **which
project the section belongs to**.

This module puts the two back together:

* an open item inherits the project of the section it lives in;
* it keeps the heading, so the page can link to the exact place in the
  note where it was written;
* the sections that say they are blocking say so in the page too.

Nothing here is invented: every line is the author's own, in their order.
What counts as an owner and as a blocking heading is configurable (see
``vault_conventions``).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db, vault_conventions

log = logging.getLogger("conductloop.openwork")

# An open checkbox: "- [ ] something". The checked ones are work that is
# already done, which is not what this is about.
_OPEN_BOX = re.compile(r"^\s*[-*]\s+\[\s\]\s+(.+?)\s*$")

# A date in a heading: "estado real: 24-oct, 22:45", "2026-10-23 — ...".
_DATES = (
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})-([a-z]{3})[a-z]*\b", re.IGNORECASE),
)
# Month names of a heading date ("24-oct", "24-Oct"): Spanish and English
# abbreviations both count, so a vault in either language is read.
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"])}
_MONTHS.update({"jan": 1, "apr": 4, "aug": 8, "dec": 12})

# Headings that say these items are blocking (English and Spanish). More
# patterns can be added with ``blocking_headings`` in the config.
_BLOCKING_BASE = (
    r"bloqueant|blocking|blocker|open question|pregunta abierta|"
    r"decisi[oó]n pendiente|pending decision|esperando|waiting"
)


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


def _fingerprint(text: str) -> str:
    """A comparable form of a line, so the same item twice looks the same."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


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

    conv = vault_conventions.get()
    owner_re = conv.owner_re()
    blocking_re = conv.blocking_re(_BLOCKING_BASE)
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
            item = match.group(1).strip()
            # The section names the project; when it does not, the item
            # usually names it itself ("Acme: test on a real Windows
            # server"). Both are the author's own words.
            key = section_key or _project_of(item)
            if not key:
                continue
            owner = ""
            found = owner_re.search(item)
            if found:
                owner = found.group(1)
                item = item[:found.start()].strip()
            out.setdefault(key, []).append({
                "path": path,
                "project": key,
                "section": heading,
                "date": _section_date(heading, year),
                "blocking": bool(blocking_re.search(heading)),
                "owner": owner,
                "text": item[:300],
            })

    for key, rows in out.items():
        # The same item written twice is one item. It happens: TODO.md
        # repeats its own first half, and every pending shows up twice.
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, Any]] = []
        for row in rows:
            mark = (row["section"], _fingerprint(row["text"]))
            if mark in seen:
                continue
            seen.add(mark)
            unique.append(row)
        rows = unique
        # Newest section first, and inside a section the order it was written.
        # What blocks comes on top of that: two stable sorts.
        rows.sort(key=lambda row: (row["date"] or "9999"), reverse=True)
        rows.sort(key=lambda row: not row["blocking"])
        if limit_per_project:
            out[key] = rows[:limit_per_project]
        else:
            out[key] = rows
    _ = notes_structure  # the import keeps the module honest about its dependency
    return out


def counts() -> dict[str, int]:
    """How many things are open, per project."""
    return {key: len(rows) for key, rows in scan().items()}
