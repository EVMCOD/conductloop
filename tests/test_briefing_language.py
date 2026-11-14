"""The morning briefing speaks the language the person chose.

The note lands in the person's own vault every morning and the panel shows it
in the "Now" view, so it cannot be written in our language: it is written in
theirs. The marker that finds the previous block of the day does not change
with the language, or somebody who switched language would end up with two
briefings in the same note.
"""
from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "b.db"))
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    from ops_dashboard import brain, db, i18n

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "b.db")
    monkeypatch.setattr(brain, "CONFIG_PATH", tmp_path / "config.json")
    db.init_db()
    i18n.set_request_language(None)
    return db, tmp_path


def test_the_briefing_is_written_in_the_language_the_person_chose(env):
    from ops_dashboard import briefing, i18n

    i18n.set_locale("es")
    text = briefing.compose(date(2026, 7, 21))
    assert "Resumen" in text
    assert "TODOs abiertos" in text
    assert "Generado" in text
    assert "Open TODOs" not in text
    assert "Generated" not in text


def test_the_briefing_speaks_english_when_that_is_the_choice(env):
    from ops_dashboard import briefing, i18n

    i18n.set_locale("en")
    text = briefing.compose(date(2026, 7, 21))
    assert "Briefing" in text
    assert "Open TODOs" in text
    assert "TODOs abiertos" not in text


def test_the_words_of_the_person_are_not_translated(env):
    """The note says what happened; what happened is in the person's words."""
    from ops_dashboard import briefing, i18n

    i18n.set_locale("es")
    env[0].add_decision("Ship it", "go live")
    text = briefing.compose(date(2026, 7, 21))
    assert "Ship it" in text
    assert "Decisiones recientes" in text


def test_changing_the_language_replaces_the_note_instead_of_duplicating_it(env, tmp_path):
    """The note is written once a day. Somebody who switches language gets
    their note replaced, not two briefings in it."""
    from ops_dashboard import briefing, i18n

    i18n.set_locale("en")
    briefing.to_daily_note(date(2026, 7, 21))
    i18n.set_locale("es")
    briefing.to_daily_note(date(2026, 7, 21))
    note = (tmp_path / "vault" / "daily" / "2026-07-21.md").read_text(encoding="utf-8")
    assert note.count("## 🌅 ConductLoop") == 1
    assert "Resumen" in note
    assert "Briefing" not in note


def test_the_note_still_holds_the_words_the_person_wrote(env, tmp_path):
    """The marker finds the block of the briefing, not everything the person
    wrote in their own daily note."""
    from ops_dashboard import briefing, i18n

    i18n.set_locale("en")
    note = tmp_path / "vault" / "daily" / "2026-07-21.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("## My own notes\n\nSomething I wrote myself.\n", encoding="utf-8")
    briefing.to_daily_note(date(2026, 7, 21))
    text = note.read_text(encoding="utf-8")
    assert "Something I wrote myself." in text
    assert text.count("## 🌅 ConductLoop") == 1
