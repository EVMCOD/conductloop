"""Playbook entries per project.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db``.
"""
from __future__ import annotations

import re
import time

from ops_dashboard import db as _dbmod

# ---- Playbook helpers ----

PLAYBOOK_HEADER_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
PLAYBOOK_KV_RE = re.compile(r"\*\*Phase:\*\*\s*(.+?)\s*$", re.MULTILINE)
LIST_ITEM_RE = re.compile(r"^(\s*)-\s+\[([ xX])\]\s+(.+?)\s*$", re.MULTILINE)
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def replace_playbook_entries(project_key: str, entries: list[dict]) -> None:
    """Replace all entries for a project with the parsed set.

    Audit 2026-06-23: rewrote N+1 (one statement per entry) into batched
    operations. New pattern: 1 SELECT to find existing, 1 DELETE for
    removed (if any), executemany for UPDATEs, executemany for INSERTs,
    1 UPSERT for phases. N+1 became 4-5 statements regardless of N.
    """
    now = int(time.time())
    with _dbmod.db() as conn:
        # 1. Find existing (id, content) for this project
        existing = {
            r["content"]: r["id"]
            for r in conn.execute(
                "SELECT id, content FROM playbook_entries WHERE project_key = ?",
                (project_key,),
            ).fetchall()
        }
        new_contents = {e["content"] for e in entries}
        # 2. Delete removed (single statement)
        to_delete = [c for c in existing if c not in new_contents]
        if to_delete:
            conn.execute(
                f"DELETE FROM playbook_entries WHERE project_key = ? AND content IN ({','.join('?' for _ in to_delete)})",
                [project_key, *to_delete],
            )
        # 3. Batch UPDATE existing entries via executemany
        to_update = [e for e in entries if e["content"] in existing]
        if to_update:
            conn.executemany(
                "UPDATE playbook_entries SET done = ?, order_idx = ?, entry_kind = ?, updated_at = ? "
                "WHERE id = ?",
                [(1 if e["done"] else 0, e["order"], e["kind"], now, existing[e["content"]])
                 for e in to_update],
            )
        # 4. Batch INSERT new entries via executemany
        to_insert = [e for e in entries if e["content"] not in existing]
        if to_insert:
            conn.executemany(
                "INSERT INTO playbook_entries (project_key, entry_kind, content, done, order_idx, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(project_key, e["kind"], e["content"], 1 if e["done"] else 0, e["order"], now)
                 for e in to_insert],
            )
        # 5. Phase updates (one per phase entry, single statement each)
        for e in entries:
            if e["kind"] == "phase":
                conn.execute(
                    "INSERT INTO playbook_phases (project_key, phase, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(project_key) DO UPDATE SET phase = excluded.phase, updated_at = excluded.updated_at",
                    (project_key, e["content"], now),
                )


def get_playbook(project_key: str) -> dict:
    with _dbmod.db() as conn:
        phase_row = conn.execute(
            "SELECT phase, notes, updated_at FROM playbook_phases WHERE project_key = ?", (project_key,)
        ).fetchone()
        entries = conn.execute(
            "SELECT id, entry_kind, content, done, order_idx, updated_at "
            "FROM playbook_entries WHERE project_key = ? ORDER BY order_idx",
            (project_key,),
        ).fetchall()
        return {
            "project_key": project_key,
            "phase": phase_row["phase"] if phase_row else None,
            "phase_updated_at": phase_row["updated_at"] if phase_row else None,
            "entries": [dict(r) for r in entries],
        }


def all_playbooks_summary() -> list[dict]:
    with _dbmod.db() as conn:
        rows = conn.execute(
            """
            SELECT p.project_key, p.phase, p.updated_at AS phase_updated_at,
                   (SELECT COUNT(*) FROM playbook_entries e WHERE e.project_key = p.project_key) AS total,
                   (SELECT COUNT(*) FROM playbook_entries e WHERE e.project_key = p.project_key AND e.done = 1) AS done
            FROM playbook_phases p
            ORDER BY p.project_key
            """
        ).fetchall()
        return [dict(r) for r in rows]


def list_playbook_projects() -> list[str]:
    """All project keys that have a playbook entry, even without a phase row."""
    with _dbmod.db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT project_key FROM playbook_entries UNION SELECT project_key FROM playbook_phases"
        ).fetchall()
        return [r["project_key"] for r in rows]
