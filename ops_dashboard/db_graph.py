"""Cross-source relation graph.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db``.
"""
from __future__ import annotations

from ops_dashboard import db as _dbmod

# ---- Graph helpers ----

def build_relations(limit: int = 500) -> dict:
    """Build a graph of doc-doc and doc-project relations.

    For now we extract:
      - wikilinks from obsidian docs (already in metadata)
      - doc mentions of project aliases (from project aliases list)
    """
    from . import projects as projects_mod
    with _dbmod.db() as conn:
        rows = conn.execute(
            "SELECT id, source, path, title, metadata FROM documents "
            "WHERE source = 'obsidian' LIMIT ?",
            (limit,),
        ).fetchall()
    nodes = []
    edges = []
    for r in rows:
        nodes.append({"id": r["id"], "label": r["title"] or r["path"].split("/")[-1], "source": r["source"]})
        try:
            import json as _json
            meta = _json.loads(r["metadata"] or "{}")
        except Exception:
            meta = {}
        for link in meta.get("wikilinks", []):
            edges.append({"from": r["id"], "to_label": link, "type": "wikilink"})
        text = (r["title"] or "") + " " + r["path"]
        for p in projects_mod.PROJECTS:
            for alias in [p.key, p.name.lower()] + [a.lower() for a in p.aliases]:
                if alias and len(alias) >= 4 and alias in text.lower():
                    edges.append({"from": r["id"], "to_project": p.key, "type": "mentions"})
                    break
    return {"nodes": nodes, "edges": edges}


def activity_by_day(days: int = 90) -> list[dict]:
    """Return list of {day, count} for the last N days, including zero days.

    Counts = number of real events (from activity_feed) per day.
    This includes todo_added, task_started/done, agent_artifact,
    idea_added, etc. — not just document mtimes. Much more
    meaningful as a 'what did I do' signal.
    """
    import time as _t
    since_ts = int(_t.time()) - days * 86400
    with _dbmod.db() as conn:
        rows = conn.execute(
            """
            SELECT strftime('%Y-%m-%d', created_at, 'unixepoch') AS day,
                   COUNT(*) AS c
            FROM activity_feed
            WHERE created_at >= ?
            GROUP BY day
            ORDER BY day
            """,
            (since_ts,),
        ).fetchall()
    by_day = {r["day"]: r["c"] for r in rows}
    from datetime import datetime, timedelta
    today = datetime.now().date()
    out = []
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i))
        key = d.strftime("%Y-%m-%d")
        out.append({"day": key, "count": by_day.get(key, 0)})
    return out


def todo_summary() -> dict:
    with _dbmod.db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM todos").fetchone()["c"]
        open_ = conn.execute("SELECT COUNT(*) AS c FROM todos WHERE done = 0").fetchone()["c"]
        per_project = conn.execute(
            "SELECT project_key, COUNT(*) AS c, SUM(done) AS done "
            "FROM todos WHERE done = 0 GROUP BY project_key ORDER BY c DESC"
        ).fetchall()
        return {
            "total": total,
            "open": open_,
            "by_project": [dict(r) for r in per_project],
        }


# Brain (RAG) helpers moved to db_brain.py (2026-07-20). Re-exported so `db.<name>` keeps working.
