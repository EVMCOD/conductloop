"""Playbook indexer.

Reads <repo>/playbook/<key>.md (one per project), parses sections +
checklist items, and writes them into playbook_entries + playbook_phases tables.

The .md format is intentionally simple:

    # <title>
    **Phase:** Co-Pilots

    ## AI agents in use
    - [ ] item
    - [x] done item

    ## Proposed agents (next 90 days)
    - [ ] item
    ...

Section heading → entry_kind mapping is fixed. Anything not matched falls
into 'note'. Free text under a heading (no checkbox) is one 'note' entry.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import db

PLAYBOOK_DIR = Path(__file__).resolve().parent.parent.parent / "playbook"

# Section heading (case-insensitive prefix) -> entry_kind
SECTION_KIND: list[tuple[str, str]] = [
    ("ai agents in use",          "agent_in_use"),
    ("proposed agents",           "proposed_agent"),
    ("risk",                      "risk"),
    ("compliance",                "risk"),
    ("open blockers",             "blocker"),
    ("wins",                      "win"),
    ("cross-ref",                 "cross_ref"),
]

CHECKBOX_RE = re.compile(r"^\s*-\s+\[([ xX])\]\s+(.+?)\s*$")
PHASE_RE    = re.compile(r"\*\*Phase:\*\*\s*(.+?)\s*$", re.MULTILINE)


def _parse_one(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return None
    key = path.stem

    # Phase
    phase_match = PHASE_RE.search(text)
    phase = phase_match.group(1).strip() if phase_match else None

    # Walk line by line tracking current section
    entries: list[dict] = []
    if phase:
        entries.append({"kind": "phase", "content": phase, "done": False, "order": 0})

    current_section: str | None = None
    order = 1
    for line in text.splitlines():
        s = line.rstrip()
        if not s:
            continue
        if s.startswith("---"):
            continue
        if s.startswith("# "):
            continue  # title
        if s.startswith("**Phase:"):
            continue
        if s.startswith("## "):
            heading = s[3:].strip().lower()
            current_section = None
            for prefix, kind in SECTION_KIND:
                if heading.startswith(prefix):
                    current_section = kind
                    break
            continue
        if current_section is None:
            continue
        m = CHECKBOX_RE.match(s)
        if m:
            done = m.group(1).lower() == "x"
            content = m.group(2).strip()
            if content and not content.startswith("(_"):
                entries.append({"kind": current_section, "content": content, "done": done, "order": order})
                order += 1
        else:
            # Plain line under a section — treat as a note
            stripped = s.lstrip("-* ").strip()
            if stripped and not stripped.startswith("("):
                entries.append({"kind": "note", "content": stripped, "done": False, "order": order})
                order += 1
    return {"key": key, "entries": entries}


def run() -> dict:
    counters = {"indexed": 0, "skipped": 0, "errors": 0, "projects": 0}
    if not PLAYBOOK_DIR.exists():
        print(f"playbook: directory not found: {PLAYBOOK_DIR}")
        return counters

    files = sorted(PLAYBOOK_DIR.glob("*.md"))
    for path in files:
        try:
            parsed = _parse_one(path)
            if not parsed:
                counters["skipped"] += 1
                continue
            db.replace_playbook_entries(parsed["key"], parsed["entries"])
            counters["indexed"] += 1
            counters["projects"] += 1
        except Exception as e:
            counters["errors"] += 1
            print(f"  ! parse error {path}: {e}")
    print(f"playbook: indexed {counters['projects']} projects from {PLAYBOOK_DIR}")
    return counters
