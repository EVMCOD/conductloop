"""TODO/pendiente extractor.

Scans the indexed documents (and playbook entries) for unchecked checklist
items and explicit TODO / pendiente lines. Stores them in the `todos` table,
linking each to a project_key when possible (via project aliases).

Run AFTER obsidian / memory / repos / playbook indexers so we have fresh data.
"""
from __future__ import annotations

import re

from .. import db

# Match `- [ ]`, `- [x]`, `* [ ]` etc. (any leading whitespace)
CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.+?)\s*$")
# Match "TODO:" or "Pendiente:" or "Pendientes:" inline anywhere
TODO_INLINE_RE = re.compile(r"(?im)^[\s>]*((?:TODO|Pendiente|Pendientes|FIXME)\s*[:\-].{6,300})$")


def _classify(text: str, projects_mod) -> str | None:
    """Try to associate a text with a project_key. Returns key or None."""
    lower = text.lower()
    for p in projects_mod.PROJECTS:
        for alias in (p.key, p.name.lower(), *[a.lower() for a in p.aliases]):
            if alias and len(alias) >= 4 and alias in lower:
                return p.key
    return None


def _scan_doc(row: dict, projects_mod) -> list[dict]:
    """Extract todos from a single document row."""
    out: list[dict] = []
    content = row["content"]
    for line in content.splitlines():
        m = CHECKBOX_RE.match(line)
        if m:
            done = m.group(1).lower() == "x"
            text = m.group(2).strip()
            if not text or text.startswith("<!--") or text.startswith("_("):
                continue
            out.append({
                "content": text,
                "done": done,
                "doc_id": row["id"],
                "project_key": _classify(text + " " + row["path"], projects_mod),
                "mtime": row["mtime"],
            })
            continue
    for m in TODO_INLINE_RE.finditer(content):
        text = m.group(1).strip()
        if not text or text.startswith("<!--"):
            continue
        out.append({
            "content": text,
            "done": False,
            "doc_id": row["id"],
            "project_key": _classify(text + " " + row["path"], projects_mod),
            "mtime": row["mtime"],
        })
    return out


def _scan_playbook(projects_mod) -> list[dict]:
    out: list[dict] = []
    for p in projects_mod.PROJECTS:
        pb = db.get_playbook(p.key)
        if not pb["entries"]:
            continue
        for e in pb["entries"]:
            if e["entry_kind"] in ("phase", "note", "cross_ref"):
                continue
            out.append({
                "content": f"[{e['entry_kind']}] {e['content']}",
                "done": bool(e["done"]),
                "doc_id": None,
                "project_key": p.key,
                "mtime": e["updated_at"],
            })
    return out


def run() -> dict:
    from .. import projects as projects_mod
    counters = {"docs": 0, "todos": 0, "playbook": 0}
    with db.db() as conn:
        rows = conn.execute(
            "SELECT id, source, path, title, content, mtime FROM documents"
        ).fetchall()
    all_todos: list[dict] = []
    for r in rows:
        counters["docs"] += 1
        all_todos.extend(_scan_doc(dict(r), projects_mod))
    all_todos.extend(_scan_playbook(projects_mod))
    counters["todos"] = len(all_todos)

    # Replace per-source so re-runs are clean
    by_source: dict[str, list[dict]] = {"obsidian": [], "memory": [], "repo": [], "playbook": []}
    for t in all_todos:
        # The doc's source was the original; we stored that during scan
        # but we need to map from doc_id; do a second pass to grab source.
        # Cheaper: tag based on project_key (playbook) or look up source.
        if t["doc_id"] is None and t.get("project_key"):
            by_source["playbook"].append(t)
        else:
            src = next((r["source"] for r in rows if r["id"] == t["doc_id"]), None)
            if src in by_source:
                by_source[src].append(t)
    for src, items in by_source.items():
        db.replace_todos(src, items)
        if src == "playbook":
            counters["playbook"] = len(items)

    summary = db.todo_summary()
    print(f"todos: scanned {counters['docs']} docs + playbook ({counters['playbook']} entries). "
          f"open={summary['open']} of {summary['total']} total.")
    return counters
