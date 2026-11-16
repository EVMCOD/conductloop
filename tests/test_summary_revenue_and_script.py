"""The daily digest and its AI summary say when revenue is not measured, and
the summary never ships characters of a script the person does not read.

With no revenue source connected, the digest printed "€0.00 from 0 events"
every day and the AI summary told the person to look into "the 0 revenue":
not measured read as no sales. And on 15-nov-2026 a Spanish summary came back
with "GA4意識" and "tráficoajar而下" in it.
"""
from __future__ import annotations

import datetime as dt

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
    i18n.set_locale("es")
    return db


def _add_source(db):
    db.add_revenue_source("shop", "mock", "mosaic")


def test_the_digest_says_revenue_is_not_measured_when_no_source_is_connected(env):
    from ops_dashboard.indexers import digest

    md = digest.build_markdown(dt.date(2026, 11, 14))
    assert "No hay ninguna fuente de ingresos conectada" in md
    assert "0 eventos" not in md


def test_the_digest_shows_the_figures_once_a_source_is_connected(env):
    from ops_dashboard.indexers import digest

    _add_source(env)
    md = digest.build_markdown(dt.date(2026, 11, 14))
    assert "No hay ninguna fuente de ingresos conectada" not in md
    assert "0 eventos" in md


def test_the_summary_is_told_revenue_is_not_measured(env):
    from ops_dashboard.indexers import ai_summary

    ctx = ai_summary._gather_day_context(dt.date(2026, 11, 14))
    assert ctx["revenue_connected"] is False
    text = ai_summary._format_context_for_prompt(ctx)
    assert "not measured" in text and "NOT zero sales" in text
    assert "€0.00 from 0 events" not in text

    _add_source(env)
    ctx = ai_summary._gather_day_context(dt.date(2026, 11, 14))
    assert ctx["revenue_connected"] is True
    assert "€0.00 from 0 events" in ai_summary._format_context_for_prompt(ctx)


class _Provider:
    model = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.systems: list[str] = []

    def ask(self, system, messages):
        self.systems.append(system)
        return self.answers.pop(0)


def test_an_answer_that_drifts_into_another_script_is_asked_again():
    from ops_dashboard.indexers import ai_summary

    p = _Provider(["El tráfico意識 bajó.", "El tráfico bajó."])
    assert ai_summary._ask_in_language(p, "sys", "prompt", "Spanish") == "El tráfico bajó."
    assert len(p.systems) == 2
    assert "only the Spanish alphabet" in p.systems[1]


def test_a_second_drift_loses_the_stray_characters():
    from ops_dashboard.indexers import ai_summary

    p = _Provider(["GA4意識 225", "GA4意識 registró 225 usuarios; tráficoajar而下"])
    out = ai_summary._ask_in_language(p, "sys", "prompt", "Spanish")
    assert out == "GA4 registró 225 usuarios; tráficoajar"
    assert not ai_summary._FOREIGN_SCRIPT.search(out)


def test_a_clean_answer_is_asked_once():
    from ops_dashboard.indexers import ai_summary

    p = _Provider(["Todo normal. 🟢"])
    assert ai_summary._ask_in_language(p, "sys", "prompt", "Spanish") == "Todo normal. 🟢"
    assert len(p.systems) == 1
