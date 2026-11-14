"""Guards the morning briefing (#3) + ICS calendar import/agenda (#10)."""
from __future__ import annotations

from datetime import date

import pytest

_ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:evt-1
SUMMARY:Standup
DTSTART:20260721T090000Z
END:VEVENT
BEGIN:VEVENT
UID:evt-2
SUMMARY:All-day offsite
DTSTART;VALUE=DATE:20260721
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "b.db"))
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "b.db"  # type: ignore[attr-defined]
    _db.init_db()
    return _db, tmp_path


def test_calendar_import_and_agenda(env, tmp_path):
    from ops_dashboard import calendar_sync
    ics = tmp_path / "cal.ics"
    ics.write_text(_ICS)
    r = calendar_sync.import_ics(str(ics))
    assert r["imported"] == 2
    ag = calendar_sync.agenda(date(2026, 7, 21))
    titles = {a["title"] for a in ag}
    assert "Standup" in titles and "All-day offsite" in titles
    standup = next(a for a in ag if a["title"] == "Standup")
    assert standup["time"] == "09:00"


def test_briefing_compose_and_daily_note(env, tmp_path):
    db, _ = env
    db.add_decision("Ship it", "go live")
    from ops_dashboard import briefing
    text = briefing.compose(date(2026, 7, 21))
    assert "ConductLoop Briefing" in text
    assert "Recent decisions" in text and "Ship it" in text
    out = briefing.to_daily_note(date(2026, 7, 21))
    note = tmp_path / "vault" / "daily" / "2026-07-21.md"
    assert note.exists() and out["note"] == str(note)
    assert "ConductLoop Briefing" in note.read_text()


def test_briefing_is_idempotent_in_daily_note(env, tmp_path):
    from ops_dashboard import briefing
    briefing.to_daily_note(date(2026, 7, 21))
    briefing.to_daily_note(date(2026, 7, 21))
    note = tmp_path / "vault" / "daily" / "2026-07-21.md"
    # only one briefing block, not two
    assert note.read_text().count("## 🌅 ConductLoop Briefing") == 1
