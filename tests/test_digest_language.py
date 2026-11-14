"""The daily digest is written in the language of the person.

The digest is a document for the person: it is saved into the data folder and
pushed to Slack/Discord. It used to be English all the way down, whatever the
person had chosen — "# Daily digest", "## 💰 Revenue", "Yesterday", "All known
URLs healthy", "No anomalies detected".
"""
from __future__ import annotations

import datetime as dt

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "d.db"))
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    from ops_dashboard import brain, db, i18n

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "d.db")
    monkeypatch.setattr(brain, "CONFIG_PATH", tmp_path / "config.json")
    db.init_db()
    i18n.set_request_language(None)
    return db, tmp_path


def _digest(env) -> str:
    from ops_dashboard.indexers import digest

    return digest.build_markdown(dt.date(2026, 7, 21))


def test_the_digest_speaks_the_language_the_person_chose(env):
    from ops_dashboard import i18n

    i18n.set_locale("es")
    text = _digest(env)
    assert "# Digest diario — 2026-07-21" in text
    assert "## 💰 Ingresos" in text
    assert "## 📈 Tráfico (GA4)" in text
    assert "## 🟢 Salud de las URLs" in text
    assert "## 🚨 Anomalías" in text
    assert "## 💹 Margen (todo el histórico)" in text
    assert "- **Ayer**" in text
    assert "_Generado el " in text
    assert "_Panel:" in text


def test_the_digest_speaks_english_when_that_is_the_choice(env):
    from ops_dashboard import i18n

    i18n.set_locale("en")
    text = _digest(env)
    assert "# Daily digest — 2026-07-21" in text
    assert "## 💰 Revenue" in text
    assert "## 📈 Traffic (GA4)" in text
    assert "## 🟢 URL health" in text
    assert "## 🚨 Anomalies" in text
    assert "- **Yesterday**" in text
    assert "_Generated at " in text
    assert "_Dashboard:" in text


def test_no_english_is_left_in_the_spanish_digest(env):
    from ops_dashboard import i18n

    i18n.set_locale("es")
    text = _digest(env)
    for leftover in ("Daily digest", "Revenue", "Traffic (GA4)", "URL health",
                     "Anomalies", "Yesterday", "Generated at", "Margin snapshot",
                     "All known URLs healthy", "No anomalies detected", "Dashboard:"):
        assert leftover not in text, leftover


def test_both_languages_carry_the_same_words(env):
    from ops_dashboard.indexers import digest

    assert set(digest._WORDS["en"]) == set(digest._WORDS["es"])
