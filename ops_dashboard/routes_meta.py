"""Meta / health routes, migrated off server.py's if/elif onto the route
registry (upgrades #4 + #6). Importing this module registers the routes on the
shared ``router``; server.py imports it once and dispatches through it before
the legacy chain.
"""
from __future__ import annotations

import json
import os
import time

from . import db, routes_learning  # noqa: F401  (routes_learning registers the Aprendizaje routes, L3)
from .routing import router


def _version() -> str:
    # The desktop app passes its own version to the bundled server (there is no
    # package metadata inside the PyInstaller bundle).
    app = os.environ.get("CONDUCTLOOP_APP_VERSION", "").strip()
    if app:
        return app[:32]
    try:
        from importlib.metadata import version
        return version("conductloop")
    except Exception:
        return "dev"

# Connectors framework: importing the module registers the /connectors page and
# /api/connectors/* routes. Placed here (not at EOF / the import line) so it
# does not collide with the other PRs that register route modules.
from . import routes_connectors  # noqa: E402,F401


@router.get("/api/health")
@router.get("/health")
def health(handler) -> None:
    """Public health check: cheap, and 503 when the DB is unreachable so a
    server stuck with a dead DB is recycled instead of looking 'up'. Enriched
    with version + schema migration version for observability."""
    db_ok = True
    schema_version = None
    try:
        from . import migrations
        with db.get_connection() as conn:
            conn.execute("SELECT 1")
            schema_version = migrations.current_version(conn)
    except Exception:
        db_ok = False
    handler._send(
        200 if db_ok else 503,
        json.dumps({"ok": db_ok, "service": "conductloop", "db": db_ok,
                    "version": _version(), "schema_version": schema_version,
                    "ts": int(time.time())}).encode(),
        "application/json",
    )


def _json_ok(handler, obj) -> None:
    handler._send(200, json.dumps(obj, default=str).encode(), "application/json")


def _qs(handler) -> dict:
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(handler.path).query)


# Token / no-auth connectors (connectors_token/*): registers
# /api/connectors/token/*. Kept away from the routes_connectors import and
# from EOF so it doesn't collide with other PRs that register route modules.
from . import routes_connectors_token  # noqa: E402,F401


@router.get("/api/brain/provider-events")
def provider_events(handler) -> None:
    """Recent provider fallbacks/refusals (no secrets: names, models, reasons)."""
    from . import brain
    try:
        limit = int((_qs(handler).get("limit") or ["50"])[0])
    except ValueError:
        limit = 50
    _json_ok(handler, {"events": brain.recent_provider_events(max(1, min(limit, 100)))})


@router.get("/api/stats")
def stats(handler) -> None:
    _json_ok(handler, db.stats())


@router.get("/api/tags")
def tags(handler) -> None:
    _json_ok(handler, db.all_tags())


@router.get("/api/playbooks")
def playbooks(handler) -> None:
    _json_ok(handler, db.all_playbooks_summary())


@router.get("/api/activity")
def activity(handler) -> None:
    days = int((_qs(handler).get("days") or ["90"])[0])
    _json_ok(handler, db.activity_by_day(days=days))


@router.get("/api/todos/summary")
def todos_summary(handler) -> None:
    _json_ok(handler, db.todo_summary())


@router.get("/api/tasks/stats")
def tasks_stats(handler) -> None:
    _json_ok(handler, db.task_stats())


@router.get("/api/usage")
def usage(handler) -> None:
    try:
        days = int((_qs(handler).get("days") or ["30"])[0])
    except Exception:
        days = 30
    _json_ok(handler, db.usage_summary(days=days))


# In-app browser (browser design PR 4): importing registers /browser. Placed
# here, away from the import block / EOF lines other PRs touch.
from . import routes_browser  # noqa: E402,F401

# ── Setup checklist + Obsidian memory sync ───────────────────────────────

@router.get("/api/setup/checklist")
def setup_checklist(handler) -> None:
    from . import onboarding
    _json_ok(handler, onboarding.checklist())


@router.get("/api/obsidian/status")
def obsidian_status(handler) -> None:
    from . import obsidian_sync
    _json_ok(handler, obsidian_sync.status())


@router.post("/api/obsidian/export")
def obsidian_export(handler) -> None:
    from . import obsidian_sync
    try:
        _json_ok(handler, {"ok": True, **obsidian_sync.export_memory()})
    except FileNotFoundError as e:
        handler._send(400, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")


@router.post("/api/obsidian/import")
def obsidian_import(handler) -> None:
    from . import obsidian_sync
    _json_ok(handler, {"ok": True, **obsidian_sync.import_memory()})


# ── Quick-capture inbox ──────────────────────────────────────────────────

@router.get("/api/capture")
def capture_list(handler) -> None:
    from . import capture
    _json_ok(handler, {"captures": capture.list_captures(), "stats": capture.stats()})


@router.post("/api/capture")
def capture_add(handler) -> None:
    from . import capture
    try:
        data = handler._read_json()
    except Exception:
        data = {}
    try:
        cid = capture.add(data.get("content", ""), kind=data.get("kind", "note"),
                          source=data.get("source"), url=data.get("url"))
    except ValueError as e:
        handler._send(400, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")
        return
    _json_ok(handler, {"ok": True, "id": cid})


@router.post("/api/capture/<capture_id>/process")
def capture_process(handler, capture_id) -> None:
    from . import capture
    try:
        data = handler._read_json()
    except Exception:
        data = {}
    _json_ok(handler, capture.process(int(capture_id), route=data.get("route")))


# ── Smart memory: semantic recall + auto-extraction ──────────────────────

@router.get("/api/memory/recall")
def memory_recall(handler) -> None:
    from . import memory_ai
    q = (_qs(handler).get("q") or [""])[0]
    _json_ok(handler, {"query": q, "results": memory_ai.recall(q)})


@router.post("/api/memory/propose")
def memory_propose(handler) -> None:
    from . import memory_ai
    try:
        data = handler._read_json()
    except Exception:
        data = {}
    proposals = memory_ai.propose_from_text(data.get("text", ""))
    saved = memory_ai.remember_proposals(proposals) if data.get("save") else 0
    _json_ok(handler, {"proposals": proposals, "saved": saved})


@router.get("/api/brain/analytics")
def brain_analytics_route(handler) -> None:
    from . import brain_analytics
    days = int((_qs(handler).get("days") or ["30"])[0])
    _json_ok(handler, brain_analytics.analytics(days=days))


@router.post("/api/voice/autofile")
def voice_autofile_route(handler) -> None:
    from . import voice_autofile
    try:
        data = handler._read_json()
    except Exception:
        data = {}
    try:
        _json_ok(handler, {"ok": True, **voice_autofile.file_transcript(
            data.get("transcript", ""), kind=data.get("kind"), project_key=data.get("project_key"))})
    except ValueError as e:
        handler._send(400, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")


# BYOK key-storage routes live in their own module; importing it here registers
# them without touching server.py.
from . import (
    routes_byok,  # noqa: E402,F401
    routes_sessions,  # noqa: E402,F401  (registers the sessions API, PR 7)
)
