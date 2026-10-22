"""Guards the scheduled system jobs (#1) — the briefing/export that now fire on
their own crons via the scheduler tick."""
from __future__ import annotations

from datetime import datetime

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "sch.db"))
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "sch.db"  # type: ignore[attr-defined]
    _db.init_db()
    from ops_dashboard import scheduler
    return scheduler, tmp_path


def test_force_runs_briefing_and_writes_daily_note(env):
    scheduler, tmp = env
    results = scheduler.run_system_jobs(force=True)
    names = {r["system_job"]: r["ok"] for r in results}
    assert names.get("briefing") is True
    # the briefing job wrote today's daily note
    import datetime as _dt
    note = tmp / "vault" / "daily" / f"{_dt.date.today().isoformat()}.md"
    assert note.exists() and "ConductLoop Briefing" in note.read_text()


def test_briefing_fires_at_its_cron(env, monkeypatch):
    scheduler, tmp = env
    monkeypatch.setenv("BRIEFING_CRON", "0 8 * * *")
    # 08:00 matches -> runs; 08:01 in the same tick would be deduped
    at_8 = datetime(2026, 7, 21, 8, 0)
    ran = scheduler.run_system_jobs(at_8)
    assert any(r["system_job"] == "briefing" for r in ran)
    # a non-matching minute doesn't run it
    scheduler._sys_fired.clear()
    at_9 = datetime(2026, 7, 21, 9, 17)
    ran2 = {r["system_job"] for r in scheduler.run_system_jobs(at_9)}
    assert "briefing" not in ran2
