"""A cron fires once per minute, even across a restart.

The "already fired this minute" marks lived in two module dicts. A restart
inside the same minute (an update, a crash, the app relaunched by launchd)
started with them empty, so every trigger and system job whose cron matched
that minute ran a second time: a second briefing, a second paid agent wake.
"""
from __future__ import annotations

import importlib
from datetime import datetime

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "cron.db"))
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cron.db")
    db.init_db()
    from ops_dashboard import scheduler
    return db, scheduler


def _restart(scheduler):
    """What a new process sees: the module state is gone, the database is not."""
    scheduler._fired_this_minute.clear()
    scheduler._sys_fired.clear()


def test_a_trigger_does_not_refire_after_a_restart_in_the_same_minute(env, monkeypatch):
    db, scheduler = env
    team = db.create_team("cron", "Cron Team")
    agent = db.add_member(team, "Cron Agent", type="ai_agent", role="agent")
    tid = db.add_trigger(agent, "cron", "0 9 * * *", prompt="ping")
    wakes: list[int] = []
    monkeypatch.setattr(scheduler, "_wake_agent",
                        lambda aid, prompt, trigger_id=None: wakes.append(trigger_id) or {"ok": True})
    monkeypatch.setattr(scheduler, "run_system_jobs", lambda now=None, **_k: [])
    at_9 = datetime(2026, 11, 6, 9, 0, 5)
    scheduler.tick(at_9)
    assert wakes == [tid]
    _restart(scheduler)
    scheduler.tick(datetime(2026, 11, 6, 9, 0, 40))
    assert wakes == [tid], "the trigger fired twice in the same minute"
    # The next matching minute still fires.
    scheduler.tick(datetime(2026, 11, 7, 9, 0, 1))
    assert wakes == [tid, tid]


def test_a_system_job_does_not_refire_after_a_restart_in_the_same_minute(env, monkeypatch):
    db, scheduler = env
    runs: list[str] = []
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: [("briefing", "0 8 * * *", lambda: runs.append("briefing"))])
    at_8 = datetime(2026, 11, 6, 8, 0, 2)
    assert [r["system_job"] for r in scheduler.run_system_jobs(at_8)] == ["briefing"]
    _restart(scheduler)
    assert scheduler.run_system_jobs(datetime(2026, 11, 6, 8, 0, 50)) == []
    assert runs == ["briefing"]
    # force still runs it (the "run now" button), and does not use up a minute.
    assert [r["system_job"] for r in scheduler.run_system_jobs(at_8, force=True)] == ["briefing"]
    assert scheduler.run_system_jobs(datetime(2026, 11, 7, 8, 0)) != []
    assert runs == ["briefing", "briefing", "briefing"]


def test_a_reloaded_module_sees_the_same_marks(env, monkeypatch):
    db, scheduler = env
    runs: list[str] = []
    jobs = lambda: [("backup", "35 * * * *", lambda: runs.append("backup"))]  # noqa: E731
    monkeypatch.setattr(scheduler, "_system_jobs", jobs)
    scheduler.run_system_jobs(datetime(2026, 11, 6, 10, 35))
    fresh = importlib.reload(scheduler)
    monkeypatch.setattr(fresh, "_system_jobs", jobs)
    assert fresh.run_system_jobs(datetime(2026, 11, 6, 10, 35, 30)) == []
    assert runs == ["backup"]


def test_two_claims_of_one_minute_only_one_wins(env):
    db, scheduler = env
    assert scheduler._claim_minute("system:briefing", "2026-11-06 08:00") is True
    assert scheduler._claim_minute("system:briefing", "2026-11-06 08:00") is False
    assert scheduler._claim_minute("system:briefing", "2026-11-06 08:01") is True
    assert scheduler._claim_minute("trigger:1", "2026-11-06 08:01") is True


def test_a_database_error_falls_back_to_the_memory_mark(env, monkeypatch):
    """The scheduler must keep working when the mark cannot be written."""
    db, scheduler = env

    def broken():
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(db, "get_connection", broken)
    runs: list[str] = []
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: [("backup", "35 * * * *", lambda: runs.append("backup"))])
    at = datetime(2026, 11, 6, 11, 35)
    scheduler.run_system_jobs(at)
    scheduler.run_system_jobs(at)
    assert runs == ["backup"]
