"""What needs the user right now — one answer instead of a dozen views.

ConductLoop spreads the work of a day over many views: approvals waiting for
a decision, agent tasks that stopped, todos that fell due, signals from the
numbers, what happened while nobody was looking. Opening the app should not
require already knowing which of those to visit, so this module answers the
only question that matters when the app opens: *what is waiting for me?*

Two rules keep the answer honest:

* Nothing here calls the model and nothing here writes. The view has to open
  fast, and a glance at it must never change the state of the workspace.
* Every source is read defensively. A broken source costs its own section,
  never the whole answer.

The answer is a list of sections, each a list of items. Items carry the
severity that decides how loudly they ask for attention, and the view they
live in, so the panel can send the person straight there.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

# How loudly an item asks for attention. Higher wins the top of a section.
BLOCKING = 3   # it cannot move until the user decides
DUE = 2        # it is due today, or already late
SIGNAL = 1     # worth a look, nobody is waiting
CONTEXT = 0    # background: what happened, what was decided

_MAX_ACTIVITY = 8
_MAX_DECISIONS = 5
_MAX_ANOMALIES = 10
_RECENT_DECISION_DAYS = 2


def _safe(fn, default):
    """Run one source of truth. A broken one costs its section, not the page."""
    try:
        return fn()
    except Exception:
        return default


def _iso(ts) -> str | None:
    """A unix timestamp as an ISO string, or None when there is no timestamp."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).isoformat(timespec="seconds")
    except Exception:
        return None


def _item(*, id: str, kind: str, title: str, view: str, severity: int,
          detail: str | None = None, when: str | None = None,
          meta: dict | None = None) -> dict:
    """One thing that wants the user's attention."""
    return {
        "id": id,
        "kind": kind,
        "title": (title or "").strip(),
        "detail": (detail or "").strip() or None,
        "view": view,
        "severity": severity,
        "when": when,
        "meta": meta or {},
    }


def _by_urgency(items: list[dict]) -> list[dict]:
    """Most urgent first; inside a severity, the one waiting longest wins."""
    return sorted(items, key=lambda i: (-i.get("severity", 0), i.get("when") or ""))


# --- the things that cannot move without the user ---------------------------


def _approvals() -> list[dict]:
    """Agent proposals waiting for a yes or a no."""
    from . import db

    items = []
    for row in db.list_pending_approvals():
        action = row.get("approval_action") or ""
        agent = row.get("agent_name") or ""
        items.append(_item(
            id=f"approval:{row.get('id')}",
            kind="approval",
            title=row.get("summary") or action or "Pending approval",
            view="approvals",
            severity=BLOCKING,
            detail=agent,
            when=_iso(row.get("created_at")),
            meta={"agent": agent, "action": action},
        ))
    return items


def _tasks_needing_you() -> list[dict]:
    """Tasks an agent finished but could not push on its own."""
    from . import db

    items = []
    for row in db.list_tasks(status="needs_approval", limit=50):
        items.append(_item(
            id=f"task:{row.get('id')}",
            kind="task_approval",
            title=row.get("title") or "",
            view="tasks",
            severity=BLOCKING,
            detail=row.get("project_key"),
            when=_iso(row.get("updated_at")),
            meta={"project": row.get("project_key"), "status": row.get("status")},
        ))
    return items


# --- what is due ------------------------------------------------------------


def _due_todos() -> list[dict]:
    """My own todos: already late, or due before the day is over."""
    from . import db

    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    end = start + 86400
    items = []
    for row in db.list_user_todos(status="open", limit=200):
        due = row.get("due_date")
        if not due or due >= end:
            continue
        late = due < start
        items.append(_item(
            id=f"todo:{row.get('id')}",
            kind="todo_overdue" if late else "todo_due",
            title=row.get("content") or "",
            view="todo",
            severity=BLOCKING if late else DUE,
            detail=row.get("project_key"),
            when=_iso(due),
            meta={"project": row.get("project_key"),
                  "priority": row.get("priority"),
                  "overdue": late},
        ))
    return sorted(items, key=lambda i: i.get("when") or "")


# --- what is worth a look ---------------------------------------------------


def _failed_tasks() -> list[dict]:
    """Agent tasks that stopped with an error."""
    from . import db

    items = []
    for row in db.list_tasks(status="error", limit=20):
        items.append(_item(
            id=f"task:{row.get('id')}",
            kind="task_error",
            title=row.get("title") or "",
            view="tasks",
            severity=SIGNAL,
            detail=row.get("output_summary") or row.get("project_key"),
            when=_iso(row.get("updated_at")),
            meta={"project": row.get("project_key"), "status": row.get("status")},
        ))
    return items


def _anomaly_view(kind: str) -> str:
    """Where an anomaly lives: revenue, traffic, or the list of sites."""
    k = (kind or "").lower()
    if k.startswith("ga") or "traffic" in k or "session" in k:
        return "ga"
    if k.startswith("url"):
        return "files"
    return "revenue"


def _signals() -> list[dict]:
    """The numbers moved off their usual baseline."""
    from .indexers import anomalies

    result = anomalies.detect_recent_anomalies()
    items = []
    for a in (result.get("anomalies") or []):
        severity = (a.get("severity") or "").lower()
        if severity not in ("high", "medium"):
            continue
        kind = a.get("kind") or ""
        items.append(_item(
            id=f"anomaly:{kind}:{a.get('project_key') or a.get('site_key') or ''}",
            kind="anomaly",
            title=a.get("message") or a.get("label") or kind,
            view=_anomaly_view(kind),
            severity=SIGNAL,
            # The day the numbers moved — not the internal kind, which reads
            # "revenue_total" to whoever sees it.
            detail=a.get("date"),
            meta={"kind": kind, "severity": severity},
        ))
    return items[:_MAX_ANOMALIES]


# --- what happened ----------------------------------------------------------


def _daily_note() -> list[dict]:
    """The day's briefing, as the morning job wrote it.

    The scheduled process at 08:00 leaves a note in the vault; this reads it
    and never writes one. If the job has not run yet, the most recent note is
    shown instead of nothing.
    """
    from . import db

    folder = db.vault_root() / "daily"
    files = sorted(folder.glob("*.md")) if folder.is_dir() else []
    if not files:
        return []
    path = files[-1]
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    title = ""
    detail = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if not title:
                title = stripped.lstrip("#").strip()
            continue
        if title:
            detail = stripped
            break
    # The note is markdown; the panel shows a line, not the markup.
    if detail.startswith("- "):
        detail = detail[2:].strip()
    detail = detail.replace("**", "").strip()
    return [_item(
        id=f"daily_note:{path.stem}",
        kind="daily_note",
        title=title or path.stem,
        view="briefing",
        severity=CONTEXT,
        detail=detail,
        meta={"date": path.stem},
    )]


def _decisions() -> list[dict]:
    """Decisions taken in the last days — the context for everything else."""
    from . import db

    cutoff = int(time.time()) - _RECENT_DECISION_DAYS * 86400
    items = []
    for row in db.list_decisions(limit=20):
        decided = row.get("decided_at") or 0
        if decided and decided < cutoff:
            break
        items.append(_item(
            id=f"decision:{row.get('id')}",
            kind="decision",
            title=row.get("title") or "",
            view="decisions",
            severity=CONTEXT,
            detail=row.get("project_key"),
            when=_iso(decided),
            meta={"project": row.get("project_key")},
        ))
    return items[:_MAX_DECISIONS]


def _activity() -> list[dict]:
    """What happened while nobody was looking."""
    from . import db

    items = []
    for row in db.recent_activity(limit=_MAX_ACTIVITY):
        items.append(_item(
            id=f"activity:{row.get('id')}",
            kind="activity",
            title=row.get("summary") or "",
            view="activity",
            severity=CONTEXT,
            when=_iso(row.get("created_at")),
            meta={"kind": row.get("kind"), "project": row.get("project_key")},
        ))
    return items


def _system_jobs() -> list[dict]:
    """The app's own jobs that failed or stopped running.

    The app watches the URLs, the revenue and the traffic of the person. It
    did not watch itself: a job that failed or stopped running was invisible.
    """
    from . import system_jobs

    items = []
    for job in system_jobs.overview()["jobs"]:
        state = job.get("state")
        if state not in ("error", "overdue"):
            continue
        last = job.get("last") or {}
        items.append(_item(
            id=f"system_job:{job['name']}",
            kind="system_job",
            title=job.get("message") or job.get("label") or job["name"],
            # The jobs live in their own page: the "open" button goes there.
            view="/jobs",
            severity=SIGNAL,
            detail=last.get("error"),
            when=_iso(last.get("ran_at")),
            meta={"job": job["name"], "state": state},
        ))
    return items


def build() -> dict:
    """Everything that is waiting, in one call.

    Returns {ok, date, generated_at, needs_you, asks, sections}. A section is
    {id, count, items}; an item is {id, kind, title, detail, view, severity,
    when, meta}. The section ids are stable, so the panel can name them in
    the person's language without the server knowing the language.

    ``needs_you`` counts what cannot move without the person (the approvals
    and what falls due). ``asks`` counts everything that wants a look, the
    signals included: a workspace where nothing blocks but a site is down
    still has something to say.
    """
    now = datetime.now(UTC)
    blocking = _by_urgency(_safe(_approvals, []) + _safe(_tasks_needing_you, []))
    today = _by_urgency(_safe(_due_todos, []))
    signals = _by_urgency(_safe(_failed_tasks, []) + _safe(_signals, [])
                          + _safe(_system_jobs, []))
    context = _safe(_daily_note, []) + _safe(_decisions, []) + _safe(_activity, [])
    sections = [
        {"id": "blocking", "count": len(blocking), "items": blocking},
        {"id": "today", "count": len(today), "items": today},
        {"id": "signals", "count": len(signals), "items": signals},
        {"id": "context", "count": len(context), "items": context},
    ]
    # "needs_you" are the things that cannot move without the person; "asks"
    # are all the things that want a look, the signals included. The panel
    # opens on this view when there is something to answer, not only when
    # something is blocking.
    asks = sum(1 for s in sections for i in s["items"] if i["severity"] > 0)
    return {
        "ok": True,
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(timespec="seconds"),
        "needs_you": len(blocking) + len(today),
        "asks": asks,
        "sections": sections,
    }
