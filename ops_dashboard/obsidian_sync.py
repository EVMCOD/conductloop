"""Obsidian memory bridge.

Transfers ConductLoop's Brain memory (the brain_memory key/value store) to and
from the user's Obsidian vault as plain markdown, so the second brain's memory
lives alongside their notes and travels with the vault (sync, backup, git).

Each memory entry becomes one note in ``<vault>/ConductLoop Memory/<slug>.md``
with frontmatter carrying the key + timestamp; the value is the note body. The
folder is dedicated, so a sync never touches the user's other notes.

    from . import obsidian_sync
    obsidian_sync.export_memory()   # memory -> vault
    obsidian_sync.import_memory()   # vault  -> memory (merges by newest)

CLI: `ops obsidian-sync [--import]`.  API: GET/POST /api/obsidian/*.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from . import db

log = logging.getLogger("conductloop.obsidian")

_FOLDER = "ConductLoop Memory"


def _vault() -> Path:
    override = os.environ.get("OPS_MEMORY_VAULT_DIR")
    return Path(override) if override else db.vault_root()


def _memory_dir() -> Path:
    return _vault() / _FOLDER


def _slug(key: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", key.strip()).strip("-")
    return (s or "memory")[:120]


_FM = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)


def status() -> dict:
    d = _memory_dir()
    notes = list(d.glob("*.md")) if d.exists() else []
    return {
        "vault": str(_vault()),
        "memory_dir": str(d),
        "vault_exists": _vault().exists(),
        "folder_exists": d.exists(),
        "note_count": len(notes),
        "memory_count": len(db.memory_list()),
    }


def export_memory() -> dict:
    """Write every brain_memory entry to a markdown note in the vault.
    Returns {written, dir}. Creates the folder if the vault exists."""
    if not _vault().exists():
        raise FileNotFoundError(f"vault not found: {_vault()} (set OPS_VAULT_ROOT)")
    d = _memory_dir()
    d.mkdir(parents=True, exist_ok=True)
    written = 0
    for item in db.memory_list():
        key = item["key"]
        updated = int(item.get("updated_at") or time.time())
        body = item.get("value") or ""
        note = (
            f"---\nkey: {key}\ntype: conductloop-memory\n"
            f"updated: {updated}\n---\n\n{body}\n"
        )
        (d / f"{_slug(key)}.md").write_text(note, encoding="utf-8")
        written += 1
    log.info("exported %d memory notes to %s", written, d)
    return {"written": written, "dir": str(d)}


def export_decisions() -> dict:
    """Write the decision log to markdown notes in the vault (read-only mirror —
    decisions are edited in ConductLoop, not the vault)."""
    if not _vault().exists():
        raise FileNotFoundError(f"vault not found: {_vault()} (set OPS_VAULT_ROOT)")
    d = _vault() / "ConductLoop Decisions"
    d.mkdir(parents=True, exist_ok=True)
    written = 0
    for dec in db.list_decisions(limit=1000):
        title = dec.get("title") or f"decision-{dec['id']}"
        tags = ", ".join(dec.get("tags") or [])
        body = (
            f"---\ntitle: {title}\ntype: conductloop-decision\n"
            f"project: {dec.get('project_key') or ''}\ntags: [{tags}]\n"
            f"decided_at: {dec.get('decided_at') or ''}\n---\n\n"
            f"## Decision\n{dec.get('decision') or ''}\n\n"
            f"## Context\n{dec.get('context') or ''}\n\n"
            f"## Consequences\n{dec.get('consequences') or ''}\n"
        )
        (d / f"{_slug(title)}-{dec['id']}.md").write_text(body, encoding="utf-8")
        written += 1
    log.info("exported %d decision notes to %s", written, d)
    return {"written": written, "dir": str(d)}


def export_all() -> dict:
    """Export both memory and decisions to the vault."""
    return {"memory": export_memory(), "decisions": export_decisions()}


def import_memory() -> dict:
    """Read the vault's memory notes back into brain_memory. Merges by key;
    a note overwrites the stored value (the vault is the edited copy)."""
    d = _memory_dir()
    if not d.exists():
        return {"imported": 0, "dir": str(d)}
    imported = 0
    for note in sorted(d.glob("*.md")):
        try:
            text = note.read_text(encoding="utf-8")
        except Exception:
            log.warning("could not read %s", note, exc_info=True)
            continue
        m = _FM.match(text)
        if not m:
            continue
        frontmatter, body = m.group(1), m.group(2).strip()
        key = None
        for line in frontmatter.splitlines():
            if line.startswith("key:"):
                key = line.split(":", 1)[1].strip()
                break
        if not key:
            continue
        db.memory_set(key, body)
        imported += 1
    log.info("imported %d memory notes from %s", imported, d)
    return {"imported": imported, "dir": str(d)}
