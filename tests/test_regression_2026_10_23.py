"""Regression tests for the defects found in the running app on 2026-10-23.

Every test here maps to a bug that was live in the app on the reviewer's
machine (app v1.3.0, PID 49081) and was fixed the same day:

* ``GET /api/ga/forecast?metric=`` interpolated the metric straight into the
  SQL (``db_revenue.py``), so any local caller could run arbitrary SQL.
* ``POST /api/webhook/<token>`` was unreachable: the route only existed in the
  GET chain, so a real POST fell through to the routing 404.
* ``PUT/DELETE /api/agents/<id>/memory/<mid>`` ignored the agent id and wrote
  into (or deleted) another agent's memory.
* The cron matcher used Monday=0 instead of the standard Sunday=0, and did
  not understand name ranges such as ``MON-FRI`` (never matched).
* The scheduler fell back to the mock provider and logged a fake
  ``run_complete`` as if a real agent had done the work.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


# ── helpers ────────────────────────────────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _hit(url: str, method: str = "GET", payload: dict | None = None,
         timeout: float = 8.0) -> tuple[int, bytes]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """The real DashboardHandler on an ephemeral port, against a throwaway DB."""
    db_path = tmp_path_factory.mktemp("ops") / "regression.db"
    os.environ["CONDUCTLOOP_DB_PATH"] = str(db_path)

    from ops_dashboard import db
    from ops_dashboard.indexers import live_urls
    from ops_dashboard.server import DashboardHandler

    db.DB_PATH = db_path  # type: ignore[attr-defined]
    db.init_db()
    live_urls.ensure_schema()

    class _QuietHandler(DashboardHandler):
        def log_message(self, *_a):
            pass

    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", db
    finally:
        server.shutdown()


# ── 1. SQL injection in the GA forecast ────────────────────────────────────

def test_ga_forecast_rejects_injected_metric():
    """The metric is interpolated into SQL: only whitelisted columns may pass."""
    from ops_dashboard import db  # import the package entry first: db_revenue
                                  # imports db, so importing it directly first
                                  # leaves a half-initialised module behind.
    db.init_db()

    with pytest.raises(ValueError):
        db.ga_forecast(metric="(select count(*) from sqlite_master)")
    with pytest.raises(ValueError):
        db.ga_forecast(metric="users) ; DROP TABLE ga_metrics_daily --")


def test_ga_forecast_accepts_only_whitelisted_metrics():
    from ops_dashboard import db
    db.init_db()

    assert "users" in db.GA_FORECAST_METRICS
    assert "page_views" in db.GA_FORECAST_METRICS
    for metric in db.GA_FORECAST_METRICS:
        out = db.ga_forecast(metric=metric)
        assert "history" in out


def test_ga_forecast_http_rejects_bad_metric_with_400(live_server):
    """The HTTP layer answers 400 (client error), not 500."""
    base, _db = live_server
    status, body = _hit(base + "/api/ga/forecast?metric=users")
    assert status == 200, body[:200]

    status, body = _hit(
        base + "/api/ga/forecast?metric=" + urllib.parse.quote("1) UNION SELECT 1,2,3,4,5--")
    )
    assert status == 400, (status, body[:200])


# ── 2. The agent webhook must be reachable by POST ──────────────────────────

def test_webhook_post_is_reachable(live_server):
    """A POST to the webhook route reaches the dispatcher, not the router 404.

    The routing 404 answers with the bare text ``not found``; the dispatcher
    answers with JSON. An unknown token is fine — the point is that the
    handler runs at all.
    """
    base, _db = live_server
    status, body = _hit(base + "/api/webhook/no-such-token", method="POST",
                        payload={"message": "hello"})
    assert status == 404
    text = body.decode("utf-8", "ignore")
    assert text.strip() != "not found", f"routing 404 reached: {text!r}"
    assert "unknown or disabled webhook" in text, text


# ── 3. An agent may not touch another agent's memory ───────────────────────

def test_agent_memory_cannot_cross_agents(live_server):
    base, db = live_server
    team_id = db.create_team(f"regression-{os.getpid()}", "Regression Team")
    agent_a = db.add_member(team_id, "Agent A", type="ai_agent", role="agent")
    agent_b = db.add_member(team_id, "Agent B", type="ai_agent", role="agent")
    memory_id = db.add_agent_memory(agent_b, "B's private note", importance=9)

    # Agent A tries to rewrite B's memory.
    status, body = _hit(
        f"{base}/api/agents/{agent_a}/memory/{memory_id}", method="PUT",
        payload={"content": "hijacked", "importance": 1},
    )
    assert status == 404, (status, body[:200])

    # B's memory is untouched.
    mem = db.get_agent_memory(memory_id)
    assert mem["content"] == "B's private note"
    assert int(mem["importance"]) == 9

    # And B can still edit its own.
    status, _ = _hit(
        f"{base}/api/agents/{agent_b}/memory/{memory_id}", method="PUT",
        payload={"content": "B edited its own note"},
    )
    assert status == 200
    assert db.get_agent_memory(memory_id)["content"] == "B edited its own note"


# ── 4. Cron must speak standard cron ───────────────────────────────────────

def test_cron_day_of_week_uses_standard_numbering():
    """0 is Sunday and 1 is Monday, like every other cron on earth."""
    from ops_dashboard.scheduler import _cron_matches

    monday = dt.datetime(2026, 10, 26, 9, 0)
    sunday = dt.datetime(2026, 10, 25, 9, 0)

    assert _cron_matches("0 9 * * 1", monday) is True, "1 must be Monday"
    assert _cron_matches("0 9 * * 1", sunday) is False
    assert _cron_matches("0 9 * * 0", sunday) is True, "0 must be Sunday"
    assert _cron_matches("0 9 * * 0", monday) is False
    assert _cron_matches("0 9 * * 7", sunday) is True, "7 is Sunday too"


def test_cron_matches_day_name_ranges():
    from ops_dashboard.scheduler import _cron_matches

    monday = dt.datetime(2026, 10, 26, 9, 0)
    saturday = dt.datetime(2026, 10, 31, 9, 0)

    assert _cron_matches("0 9 * * MON-FRI", monday) is True, "MON-FRI must match Monday"
    assert _cron_matches("0 9 * * MON-FRI", saturday) is False
    assert _cron_matches("0 9 * * MON", monday) is True
    assert _cron_matches("0 9 * * MON,WED,FRI", monday) is True
    assert _cron_matches("0 9 * * SAT,SUN", monday) is False


def test_cron_still_handles_the_basics():
    from ops_dashboard.scheduler import _cron_matches

    assert _cron_matches("*/5 * * * *", dt.datetime(2026, 10, 26, 9, 5)) is True
    assert _cron_matches("*/5 * * * *", dt.datetime(2026, 10, 26, 9, 6)) is False
    assert _cron_matches("0 9 * * *", dt.datetime(2026, 10, 26, 9, 0)) is True
    assert _cron_matches("0 9 * * *", dt.datetime(2026, 10, 26, 9, 5)) is False
    assert _cron_matches("30 8,20 * * *", dt.datetime(2026, 10, 26, 20, 30)) is True
    assert _cron_matches("*/15 9-17 * * *", dt.datetime(2026, 10, 26, 14, 15)) is True
    assert _cron_matches("0 9 * * *", dt.datetime(2026, 10, 26, 9, 0, 0, 999999)) is True
    # Malformed expressions never match (and never raise).
    assert _cron_matches("", dt.datetime(2026, 10, 26, 9, 0)) is False
    assert _cron_matches("0 9 * *", dt.datetime(2026, 10, 26, 9, 0)) is False
    assert _cron_matches("99 99 * * *", dt.datetime(2026, 10, 26, 9, 0)) is False


def test_brain_conversations_endpoint_exists(live_server):
    """The UI calls /api/brain/conversations; it used to 404, so the agent
    panel stayed on "loading…" forever."""
    base, db = live_server
    status, raw = _hit(base + "/api/brain/conversations")
    assert status == 200, (status, raw[:200])
    body = json.loads(raw)
    assert isinstance(body, list)

    # A conversation shows up with its message count.
    conv_id = db.create_conversation("una conversación de prueba")
    db.add_message(conv_id, "user", "hola")
    db.add_message(conv_id, "assistant", "buenas")
    status, raw = _hit(base + "/api/brain/conversations")
    assert status == 200
    found = [c for c in json.loads(raw) if c["id"] == conv_id]
    assert found, raw
    assert found[0]["message_count"] == 2, found[0]
    assert found[0]["title"] == "una conversación de prueba"


def test_brain_conversations_filters_by_member(live_server):
    base, _db = live_server
    status, raw = _hit(base + "/api/brain/conversations?member_id=999999")
    assert status == 200
    assert json.loads(raw) == [], raw


# ── 5. The scheduler must never fake a run ─────────────────────────────────

def test_scheduler_does_not_fall_back_to_mock(monkeypatch, tmp_path):
    """With no provider available the wake fails loudly and logs an error.

    It used to resolve the mock provider and log ``run_complete`` — a fake
    answer persisted in agent_logs as if a real agent had done the work.
    """
    from ops_dashboard import brain, db, scheduler

    db.DB_PATH = tmp_path / "sched.db"  # type: ignore[attr-defined]
    db.init_db()
    team_id = db.create_team("sched", "Scheduler Team")
    agent_id = db.add_member(team_id, "Scheduler Test Agent", type="ai_agent", role="agent")

    class _DeadProvider:
        def available(self) -> bool:
            return False

        def ask(self, *_a, **_kw):  # pragma: no cover - must never be called
            raise AssertionError("the mock provider must not be used for a real run")

    monkeypatch.setattr(brain, "resolve_provider", lambda *_a, **_kw: _DeadProvider())

    out = scheduler._wake_agent(agent_id, "hello")

    assert out.get("ok") is not True, out
    logs = db.list_agent_logs(agent_id, limit=20)
    actions = [row["action"] for row in logs]
    assert "run_complete" not in actions, actions
    assert "error" in actions, actions
