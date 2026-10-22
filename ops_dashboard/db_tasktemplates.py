"""Task templates + seeding.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

from ops_dashboard import db as _dbmod

# ============================================================
# Task templates
# ============================================================

def upsert_template(key: str, name: str, items: list[dict], *, description: str | None = None, icon: str | None = None) -> int:
    now = int(__import__("time").time())
    with _dbmod.db() as conn:
        existing = conn.execute("SELECT id FROM task_templates WHERE key = ?", (key,)).fetchone()
        items_json = __import__("json").dumps(items, ensure_ascii=False)
        if existing:
            conn.execute(
                """UPDATE task_templates
                   SET name=?, description=?, icon=?, items=?, updated_at=?
                   WHERE key=?""",
                (name, description, icon, items_json, now, key),
            )
            return int(existing["id"])
        else:
            cur = conn.execute(
                """INSERT INTO task_templates (key, name, description, icon, items, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (key, name, description, icon, items_json, now, now),
            )
            return int(cur.lastrowid or 0)


def list_templates() -> list[dict]:
    with _dbmod.db() as conn:
        rows = conn.execute("SELECT * FROM task_templates ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["items"] = __import__("json").loads(d.get("items") or "[]")
        except Exception: d["items"] = []
        out.append(d)
    return out


def get_template(key: str) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM task_templates WHERE key = ?", (key,)).fetchone()
    if not row: return None
    d = dict(row)
    try: d["items"] = __import__("json").loads(d.get("items") or "[]")
    except Exception: d["items"] = []
    return d


def delete_template(key: str) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM task_templates WHERE key = ?", (key,))
        return cur.rowcount > 0


def instantiate_template(key: str, project_key: str | None = None, *, as_user_todos: bool = True) -> list[int]:
    """Create one user_todo per item in the template. Returns list of created ids."""
    tmpl = get_template(key)
    if not tmpl: return []
    ids: list[int] = []
    for it in tmpl["items"]:
        if not isinstance(it, dict): continue
        title = it.get("title") or it.get("text") or ""
        if not title: continue
        desc = it.get("description")
        full = title + (f" — {desc}" if desc else "")
        tags = [f"template:{key}"]
        if project_key: tags.append(f"project:{project_key}")
        todo_id = _dbmod.add_user_todo(content=full, project_key=project_key, tags=tags, priority=1 if it.get("important") else 0)
        ids.append(todo_id)
    return ids


