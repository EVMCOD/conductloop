"""The anomaly messages speak the language of the person.

The panel shows these messages as they are — the alerts banner at the top and
the "Worth a look" section of the "Now" view — so the server has to write them
in the language the person chose.

They used to carry the internal kind ("revenue_total", "ga_users_acme") and
English words around it, so the person read "revenue_total ↓ -71% vs 7d
baseline (12 vs avg 43)" in an app that had just promised to speak their
language.
"""
from __future__ import annotations

import datetime as dt

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    from ops_dashboard import brain, db, i18n

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "a.db")
    monkeypatch.setattr(brain, "CONFIG_PATH", tmp_path / "config.json")
    db.init_db()
    i18n.set_request_language(None)
    return db, tmp_path


def _series(values, last_day):
    days = [last_day - dt.timedelta(days=len(values) - 1 - i) for i in range(len(values))]
    return [{"date": d.isoformat(), "x": i, "y": v} for i, (d, v) in enumerate(zip(days, values))]


def _drop():
    """A finished day with a real drop: 230 users a day, then 66."""
    yesterday = min(dt.date.today(), dt.datetime.now(dt.UTC).date()) - dt.timedelta(days=1)
    return _series([230, 225, 240, 228, 235, 222, 231, 66], yesterday)


def test_the_anomaly_speaks_the_language_the_person_chose(env):
    from ops_dashboard import i18n
    from ops_dashboard.indexers import anomalies

    i18n.set_locale("es")
    found = anomalies._check_series(_drop(), "y", "ga_users_total", "all")
    assert len(found) == 1
    message = found[0]["message"]
    assert "Usuarios totales de GA" in message
    assert "frente a la media de 7 días" in message
    assert "vs 7d baseline" not in message


def test_the_anomaly_speaks_english_when_that_is_the_choice(env):
    from ops_dashboard import i18n
    from ops_dashboard.indexers import anomalies

    i18n.set_locale("en")
    found = anomalies._check_series(_drop(), "y", "ga_users_total", "all")
    assert len(found) == 1
    message = found[0]["message"]
    assert "Total GA users" in message
    assert "vs 7d baseline" in message
    assert "frente a la media" not in message


def test_the_message_never_shows_the_internal_kind(env):
    """The kind is for the app. The person sees words."""
    from ops_dashboard import i18n
    from ops_dashboard.indexers import anomalies

    for locale in ("es", "en"):
        i18n.set_locale(locale)
        found = anomalies._check_series(_drop(), "y", "ga_users_total", "all")
        assert found[0]["message"]
        for internal in ("revenue_total", "ga_users_total", "ga_users_", "revenue_"):
            assert internal not in found[0]["message"], internal
        # ...and the kind itself is still there for the app to route on.
        assert found[0]["kind"] == "ga_users_total"


def test_a_project_anomaly_is_named_after_the_project(env):
    from ops_dashboard import i18n
    from ops_dashboard.indexers import anomalies

    i18n.set_locale("es")
    label = anomalies._metric_label("revenue", "Acme")
    assert label == "Ingresos de Acme"
    found = anomalies._check_series(_drop(), "y", "revenue_acme", "acme", label)
    assert found[0]["message"].startswith("Ingresos de Acme")
    assert found[0]["label"] == "Ingresos de Acme"


def test_the_url_message_speaks_the_language(env):
    from ops_dashboard import i18n
    from ops_dashboard.indexers import anomalies

    i18n.set_locale("es")
    w = anomalies._words()
    assert w["url_unhealthy"].format(name="acme.example", status="HTTP 500") == \
        "URL acme.example no responde: HTTP 500"
    assert w["no_response"] == "sin respuesta"

    i18n.set_locale("en")
    w = anomalies._words()
    assert w["url_unhealthy"].format(name="acme.example", status="HTTP 500") == \
        "URL acme.example unhealthy: HTTP 500"


def test_both_languages_carry_the_same_words(env):
    from ops_dashboard.indexers import anomalies

    assert set(anomalies._WORDS["en"]) == set(anomalies._WORDS["es"])


def test_the_now_view_does_not_show_the_internal_kind_as_its_detail(env, monkeypatch):
    """The subtitle of a signal used to be "revenue_total"."""
    from ops_dashboard import i18n, now
    from ops_dashboard.indexers import anomalies

    monkeypatch.setattr(
        anomalies, "detect_recent_anomalies",
        lambda: {"anomalies": [{
            "kind": "revenue_total", "severity": "high", "project": "all",
            "date": "2026-11-10", "label": "Ingresos totales",
            "message": "Ingresos totales ↓ -71% frente a la media de 7 días (12 vs 43)",
        }]},
    )
    i18n.set_locale("es")
    items = now._signals()
    assert len(items) == 1
    assert items[0]["detail"] == "2026-11-10"
    assert items[0]["detail"] != "revenue_total"
    assert items[0]["title"].startswith("Ingresos totales")
