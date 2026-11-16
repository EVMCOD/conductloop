"""A single timeout is not a website that fell.

The URL watch checks every URL every five minutes and records every check.
The alert that says a site is down used to read the *current* state of each
URL — the table that keeps only the last check — so a single 10-second
timeout was announced as "the site does not answer", at high severity, in the
digest of the morning. Five minutes later the same site answered 200 and the
wrong alert was still on the record.

A URL is down when its last two checks failed. The second check comes from
the history of the checks (`live_url_checks`), which keeps one row per check;
the current state alone cannot tell a timeout from an outage.
"""
from __future__ import annotations

import datetime as dt

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "urls.db"))
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    from ops_dashboard import brain, db, i18n

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "urls.db")
    monkeypatch.setattr(brain, "CONFIG_PATH", tmp_path / "config.json")
    db.init_db()
    i18n.set_request_language(None)
    return db, tmp_path


def _check(env, name: str, *, project: str, status_code: int | None, ok: bool = False) -> None:
    """One check of one URL, the way the indexer records it."""
    from ops_dashboard.indexers import live_urls

    url = "https://" + name + ".example"
    error = None
    if not ok:
        error = "TimeoutException: timed out"
        if status_code:
            error = "HTTPError: " + str(status_code)
    live_urls.ensure_schema()
    live_urls._record(
        {"name": name, "url": url, "project": project, "kind": "site"},
        {
            "status_code": status_code,
            "ok": 1 if ok else 0,
            "latency_ms": 42,
            "error": error,
            "final_url": url,
        },
    )


def _unhealthy(env) -> list[dict]:
    """The URLs the health check reports as down."""
    from ops_dashboard.indexers import live_urls

    return live_urls.recent_unhealthy()


def _alerts(env) -> list[dict]:
    """The URL health alerts the anomaly detector would raise."""
    from ops_dashboard.indexers import anomalies

    found = anomalies.detect_recent_anomalies()
    return [a for a in found["anomalies"] if a.get("kind") == "url_health"]


def test_a_timeout_followed_by_a_good_check_is_not_an_alert(env):
    """The site answered five minutes later: there was no outage."""
    _check(env, "mosaic", project="mosaic", status_code=None)
    _check(env, "mosaic", project="mosaic", status_code=200, ok=True)

    assert _unhealthy(env) == []
    assert _alerts(env) == []


def test_two_timeouts_in_a_row_are_a_high_alert(env):
    """No answer twice in a row: the site is down, and nobody answered."""
    _check(env, "mosaic", project="mosaic", status_code=None)
    _check(env, "mosaic", project="mosaic", status_code=None)

    rows = _unhealthy(env)
    assert [r["name"] for r in rows] == ["mosaic"]
    assert rows[0]["project"] == "mosaic"
    assert rows[0]["url"] == "https://mosaic.example"

    alerts = _alerts(env)
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "high"
    assert alerts[0]["name"] == "mosaic"
    assert alerts[0]["project"] == "mosaic"
    assert "no response" in alerts[0]["message"]


def test_two_http_errors_in_a_row_are_a_medium_alert(env):
    """The site answered with an error: it is down, but it answered."""
    _check(env, "tidepool", project="tidepool", status_code=500)
    _check(env, "tidepool", project="tidepool", status_code=500)

    alerts = _alerts(env)
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "medium"
    assert alerts[0]["name"] == "tidepool"
    assert "HTTP 500" in alerts[0]["message"]


def test_a_good_check_followed_by_a_timeout_is_not_an_alert_yet(env):
    """The first failure after a good check is not an outage yet."""
    _check(env, "tidepool", project="tidepool", status_code=200, ok=True)
    _check(env, "tidepool", project="tidepool", status_code=None)

    assert _unhealthy(env) == []
    assert _alerts(env) == []


def test_a_url_checked_once_and_failed_is_not_an_alert(env):
    """One check is not a history.

    Saying nothing here is the right answer: the watch checks every URL every
    five minutes, so the second check is five minutes away, and the alert
    appears then if the site is really down. Waiting for it is the whole
    point of the rule — a URL that has only ever been checked once cannot be
    called down on the strength of that single check.
    """
    _check(env, "mosaic", project="mosaic", status_code=None)

    assert _unhealthy(env) == []
    assert _alerts(env) == []


def test_the_digest_does_not_announce_a_site_that_answered_after_the_timeout(env):
    """The morning digest is where the wrong alert was read."""
    from ops_dashboard.indexers import digest

    _check(env, "mosaic", project="mosaic", status_code=None)
    _check(env, "mosaic", project="mosaic", status_code=200, ok=True)

    text = digest.build_markdown(dt.date(2026, 11, 16))
    assert "mosaic" not in text
    assert "All known URLs healthy" in text


def test_the_digest_announces_a_site_that_failed_twice(env):
    """The rule did not make the digest blind: a real outage is still there."""
    from ops_dashboard.indexers import digest

    _check(env, "mosaic", project="mosaic", status_code=None)
    _check(env, "mosaic", project="mosaic", status_code=None)

    text = digest.build_markdown(dt.date(2026, 11, 16))
    assert "mosaic" in text
    assert "1 unhealthy in last 24h" in text
