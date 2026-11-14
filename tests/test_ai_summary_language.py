"""The daily AI summary is written in the language the person chose.

The prompt used to ask for the summary "in Spanish" — always, whatever the
person had chosen — and to write the three paragraphs under Spanish headings
("Lo que suscripción"). The system prompt, at the same time, asked the model
to "match the user's language", which tells a model nothing.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    from ops_dashboard import brain, db, i18n

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "s.db")
    monkeypatch.setattr(brain, "CONFIG_PATH", tmp_path / "config.json")
    db.init_db()
    i18n.set_request_language(None)
    return db, tmp_path


def _capture(env, monkeypatch) -> dict:
    """Run the summary against a fake provider and keep what it was asked."""
    from ops_dashboard import brain
    from ops_dashboard.indexers import ai_summary

    seen: dict[str, str] = {}

    class FakeProvider:
        name = "anthropic"
        model = "fake"

        def ask(self, system, messages):
            seen["system"] = system
            seen["prompt"] = messages[0]["content"]
            return "Resumen."

    monkeypatch.setattr(brain, "resolve_provider", lambda *a, **k: FakeProvider())
    monkeypatch.setattr(ai_summary, "_gather_day_context", lambda target: {
        "date": target.isoformat(),
        "revenue": {"yesterday": {"total_cents": 0, "count": 0, "by_project": [], "by_type": []},
                    "last_7d": {"total_cents": 0, "count": 0, "by_project": [], "by_type": []},
                    "prev_7d": {"total_cents": 0, "count": 0, "by_project": [], "by_type": []}},
        "ga": {"yesterday": [], "last_14d": []},
        "sentry": {"summary": {"total": 0, "by_level": {}}, "new_today": []},
        "anomalies": {"anomalies": [], "count": 0,
                      "by_severity": {"high": 0, "medium": 0, "info": 0}},
        "activity_recent": [],
        "goals": [], "tasks_recent": [],
    })
    ai_summary.generate_ai_summary()
    return seen


def test_the_summary_is_asked_for_in_the_language_the_person_chose(env, monkeypatch):
    from ops_dashboard import i18n

    i18n.set_locale("es")
    seen = _capture(env, monkeypatch)
    assert "in Spanish" in seen["prompt"]
    assert "Lo que sucedió" in seen["prompt"]
    assert "Lo que necesita atención" in seen["prompt"]
    assert "Lo que deberías hacer mañana" in seen["prompt"]


def test_the_summary_in_english_asks_for_english(env, monkeypatch):
    from ops_dashboard import i18n

    i18n.set_locale("en")
    seen = _capture(env, monkeypatch)
    assert "in English" in seen["prompt"]
    assert "What happened" in seen["prompt"]
    assert "What needs attention" in seen["prompt"]
    assert "Lo que sucedió" not in seen["prompt"]


def test_the_system_prompt_names_the_language_too(env, monkeypatch):
    """'Match the user's language' tells a model nothing."""
    from ops_dashboard import i18n

    i18n.set_locale("es")
    seen = _capture(env, monkeypatch)
    assert "Write it in Spanish" in seen["system"]
    assert "Match the user's language" not in seen["system"]


def test_both_languages_carry_the_same_words(env):
    from ops_dashboard.indexers import ai_summary

    assert set(ai_summary._SUMMARY_WORDS["en"]) == set(ai_summary._SUMMARY_WORDS["es"])
