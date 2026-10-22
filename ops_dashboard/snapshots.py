"""Daily & weekly snapshots, plus diff between consecutive snapshots.

Called automatically after `python -m ops_dashboard index` (via the indexer pipeline)
or explicitly via `ops snapshot take`.

Snapshot payload (stored as JSON in snapshots table):
  {
    "stats": {"total": N, "per_source": [{source, c}, ...]},
    "url_status": [{name, ok, status_code, latency_ms}, ...],
    "projects": [{key, name, repos, urls, last_activity, all_urls_ok, mention_count, url_ok, url_total}, ...],
    "todos": {"total": N, "open": N, "by_project": [{project_key, c, done}, ...]}
  }

Diff highlights:
  - new docs (delta in totals)
  - URLs that went from ok->fail or fail->ok
  - projects that became active / inactive
  - open todos delta
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from . import app_paths as _app_paths
from . import db, projects
from .indexers import live_urls

SNAPSHOT_DIR = _app_paths.path("snapshots")


def take(kind: str = "auto") -> dict:
    """Take a snapshot of the current state. Returns the saved payload."""
    day = datetime.now().strftime("%Y-%m-%d")
    snap = {
        "stats": db.stats(),
        "url_status": live_urls.latest(),
        "projects": projects.all_projects_summary(),
        "todos": db.todo_summary(),
    }
    db.save_snapshot(day, kind, snap)
    return {"day": day, "kind": kind, "saved": True}


def diff(day: str | None = None, kind: str = "auto") -> dict:
    """Compute diff between `day`'s snapshot and the previous one.
    If day is None, uses today.
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    cur = db.get_snapshot(day, kind)
    if not cur:
        # Take one now
        take(kind)
        cur = db.get_snapshot(day, kind)
    prev = db.get_previous_snapshot(day, kind)
    if not prev:
        return {"day": day, "has_previous": False, "current": cur, "previous": None, "changes": []}

    changes: list[dict] = []

    # 1. Total docs delta
    cur_total = cur["stats"].get("total", 0)
    prev_total = prev["stats"].get("total", 0)
    if cur_total != prev_total:
        changes.append({
            "type": "docs_total",
            "label": f"documentos indexados: {prev_total} → {cur_total}",
            "delta": cur_total - prev_total,
        })

    # 2. Per-source docs delta
    prev_by_src = {s["source"]: s["c"] for s in prev["stats"].get("per_source", [])}
    for s in cur["stats"].get("per_source", []):
        ps = prev_by_src.get(s["source"], 0)
        if s["c"] != ps:
            changes.append({
                "type": "docs_source",
                "label": f"  {s['source']}: {ps} → {s['c']}",
                "delta": s["c"] - ps,
            })

    # 3. URL status changes
    prev_urls = {u["name"]: u["ok"] for u in prev["url_status"]}
    for u in cur["url_status"]:
        prev_ok = prev_urls.get(u["name"])
        cur_ok = u["ok"]
        if prev_ok is None:
            continue
        if cur_ok and not prev_ok:
            changes.append({"type": "url_recovered", "label": f"🟢 {u['name']} recovered", "delta": 0})
        elif not cur_ok and prev_ok:
            changes.append({"type": "url_failed", "label": f"🔴 {u['name']} failed", "delta": 0})

    # 4. Open todos delta
    cur_open = cur["todos"].get("open", 0)
    prev_open = prev["todos"].get("open", 0)
    if cur_open != prev_open:
        sign = "+" if cur_open > prev_open else "−"
        changes.append({
            "type": "todos_open",
            "label": f"TODOs abiertos: {prev_open} → {cur_open} ({sign}{abs(cur_open - prev_open)})",
            "delta": cur_open - prev_open,
        })

    # 5. Project activity changes (last_activity moved significantly)
    prev_proj = {p["key"]: p for p in prev["projects"]}
    for p in cur["projects"]:
        pp = prev_proj.get(p["key"])
        if not pp:
            continue
        if p["last_activity"] != pp["last_activity"]:
            # Activity moved
            changes.append({
                "type": "project_activity",
                "label": f"  {p['name']} ({p['key']}): actividad actualizada",
                "delta": 0,
            })

    return {
        "day": day,
        "has_previous": True,
        "previous_day": prev["day"],
        "changes": changes,
        "current": cur,
        "previous": prev,
    }


def render_markdown(d: dict) -> str:
    """Render a diff as a human-readable markdown block."""
    lines = [f"# Daily diff — {d['day']}"]
    if not d.get("has_previous"):
        lines.append("")
        lines.append("_No hay snapshot anterior — este es el primero._")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"_Comparado con snapshot del {d.get('previous_day')}_")
    lines.append("")
    if not d["changes"]:
        lines.append("Sin cambios desde ayer. ✨")
        return "\n".join(lines)
    for c in d["changes"]:
        icon = "🆕" if c["type"] == "docs_total" else \
               "📄" if c["type"] == "docs_source" else \
               "✅" if c["type"] == "url_recovered" else \
               "❌" if c["type"] == "url_failed" else \
               "📋" if c["type"] == "todos_open" else \
               "🔄" if c["type"] == "project_activity" else "·"
        lines.append(f"- {icon} {c['label']}")
    return "\n".join(lines)


def render_weekly(week_end_day: str | None = None) -> str:
    """Render a weekly summary by aggregating the last 7 daily snapshots."""
    week_end_day = week_end_day or datetime.now().strftime("%Y-%m-%d")
    end = datetime.strptime(week_end_day, "%Y-%m-%d")
    start = end - timedelta(days=6)
    lines = [f"# Weekly snapshot — {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}"]
    lines.append("")
    rows = []
    cur = end
    while cur >= start:
        d = cur.strftime("%Y-%m-%d")
        s = db.get_snapshot(d, "auto")
        if s:
            rows.append((d, s))
        cur -= timedelta(days=1)
    if not rows:
        lines.append("_Sin snapshots en este rango. Corre `ops snapshot take`._")
        return "\n".join(lines)
    rows.reverse()
    # Totals
    total_docs_end = rows[-1][1]["stats"].get("total", 0)
    total_docs_start = rows[0][1]["stats"].get("total", 0)
    open_end = rows[-1][1]["todos"].get("open", 0)
    open_start = rows[0][1]["todos"].get("open", 0)
    lines.append(f"- Documentos: {total_docs_start} → {total_docs_end} ({total_docs_end - total_docs_start:+d})")
    lines.append(f"- TODOs abiertos: {open_start} → {open_end} ({open_end - open_start:+d})")
    # Daily delta
    lines.append("")
    lines.append("## Día a día")
    lines.append("")
    lines.append("| día | docs | TODOs abiertos | URLs up |")
    lines.append("|---|---|---|---|")
    for d, s in rows:
        up = sum(1 for u in s["url_status"] if u.get("ok"))
        tot = len(s["url_status"])
        lines.append(f"| {d} | {s['stats'].get('total', 0)} | {s['todos'].get('open', 0)} | {up}/{tot} |")
    return "\n".join(lines)


def write_snapshot_file(d: dict, kind: str = "auto") -> Path:
    """Write a snapshot to SNAPSHOT_DIR/<day>.<kind>.md."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out = SNAPSHOT_DIR / f"{d['day']}.{kind}.md"
    out.write_text(render_markdown(d), encoding="utf-8")
    return out


def write_weekly_file(week_end_day: str | None = None) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    week_end_day = week_end_day or datetime.now().strftime("%Y-%m-%d")
    out = SNAPSHOT_DIR / f"week-{week_end_day}.md"
    out.write_text(render_weekly(week_end_day), encoding="utf-8")
    return out
