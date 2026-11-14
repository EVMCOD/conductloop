"""A scheduled wake cannot hang the scheduler.

``_wake_agent`` called ``provider.ask`` with no deadline. The scheduler ticks
on one thread, so one provider call that never returned stopped every later
cron trigger and system job (briefing, backup) until the app restarted.
"""
from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "wake.db"))
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "wake.db")
    db.init_db()
    team_id = db.create_team("wake", "Wake Team")
    agent_id = db.add_member(team_id, "Wake Test Agent", type="ai_agent", role="agent")
    return db, agent_id


class _HangingProvider:
    name = "hanging"

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()

    def available(self) -> bool:
        return True

    def ask(self, *_a, **_kw) -> str:
        self.started.set()
        self.release.wait(10)
        return "late answer"


def test_a_provider_that_never_answers_times_out_and_is_logged(env, monkeypatch):
    from ops_dashboard import brain, scheduler
    db, agent_id = env
    prov = _HangingProvider()
    monkeypatch.setattr(brain, "resolve_provider", lambda *_a, **_kw: prov)
    monkeypatch.setenv("OPS_AGENT_WAKE_TIMEOUT", "0.3")
    t0 = time.monotonic()
    try:
        out = scheduler._wake_agent(agent_id, "hello")
    finally:
        prov.release.set()
    elapsed = time.monotonic() - t0
    assert elapsed < 5, elapsed
    assert prov.started.is_set()
    assert out["ok"] is False
    assert out.get("timeout") is True
    assert "did not answer within" in out["error"]
    actions = [r["action"] for r in db.list_agent_logs(agent_id, limit=20)]
    assert "error" in actions and "run_complete" not in actions
    # The abandoned call finishing later must not be recorded as a run.
    time.sleep(0.2)
    actions = [r["action"] for r in db.list_agent_logs(agent_id, limit=20)]
    assert "run_complete" not in actions


def test_a_timed_out_trigger_is_recorded_as_an_error_and_the_tick_goes_on(env, monkeypatch):
    from datetime import datetime

    from ops_dashboard import brain, scheduler
    db, agent_id = env
    prov = _HangingProvider()
    monkeypatch.setattr(brain, "resolve_provider", lambda *_a, **_kw: prov)
    monkeypatch.setenv("OPS_AGENT_WAKE_TIMEOUT", "0.3")
    monkeypatch.setattr(scheduler, "run_system_jobs", lambda now=None, **_k: [])
    tid = db.add_trigger(agent_id, "cron", "* * * * *", prompt="ping")
    monkeypatch.setattr(scheduler, "_fired_this_minute", {})
    try:
        out = scheduler.tick(datetime(2026, 11, 6, 9, 0))
    finally:
        prov.release.set()
    fired = [r for r in out if r.get("trigger_id") == tid]
    assert fired and fired[0]["ok"] is False and fired[0].get("timeout") is True
    trig = next(t for t in db.list_all_enabled_triggers() if t["id"] == tid)
    assert trig["last_status"] == "error"
    assert "did not answer within" in (trig["last_error"] or "")


def test_a_normal_answer_is_unchanged(env, monkeypatch):
    from ops_dashboard import brain, scheduler
    db, agent_id = env

    class _Quick:
        name = "quick"

        def available(self) -> bool:
            return True

        def ask(self, *_a, **_kw) -> str:
            return "all good"

    monkeypatch.setattr(brain, "resolve_provider", lambda *_a, **_kw: _Quick())
    out = scheduler._wake_agent(agent_id, "hello")
    assert out["ok"] is True and out["response"] == "all good"
    actions = [r["action"] for r in db.list_agent_logs(agent_id, limit=20)]
    assert "run_complete" in actions


@pytest.mark.parametrize("raw, expected", [
    (None, 90.0), ("", 90.0), ("abc", 90.0), ("-3", 90.0), ("0", 90.0),
    ("12.5", 12.5), ("100000", 3600.0),
])
def test_the_timeout_setting_is_read_safely(monkeypatch, raw, expected):
    from ops_dashboard import brain, scheduler
    monkeypatch.setattr(brain, "LLM_TIMEOUT", 90.0)
    if raw is None:
        monkeypatch.delenv("OPS_AGENT_WAKE_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("OPS_AGENT_WAKE_TIMEOUT", raw)
    assert scheduler._wake_timeout() == expected
