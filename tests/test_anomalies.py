"""Anomaly alerts: only finished days are compared, and the feed is not flooded."""
from __future__ import annotations

import datetime as dt

import pytest

from ops_dashboard.indexers import anomalies


def _series(values, last_day):
    days = [last_day - dt.timedelta(days=len(values) - 1 - i) for i in range(len(values))]
    return [{"date": d.isoformat(), "x": i, "y": v} for i, (d, v) in enumerate(zip(days, values))]


def test_todays_partial_day_is_not_a_drop():
    today = dt.date.today()
    # seven normal days, then a morning-only count for today
    series = _series([230, 225, 240, 228, 235, 222, 231, 66], today)
    assert anomalies._check_series(series, "y", "ga_users_total", "all") == []


def test_a_real_drop_on_a_finished_day_is_still_flagged():
    yesterday = min(dt.date.today(), dt.datetime.now(dt.UTC).date()) - dt.timedelta(days=1)
    series = _series([230, 225, 240, 228, 235, 222, 231, 66], yesterday)
    found = anomalies._check_series(series, "y", "ga_users_total", "all")
    assert len(found) == 1 and found[0]["direction"] == "drop"
    assert found[0]["date"] == yesterday.isoformat()


@pytest.fixture()
def feed_db(tmp_path, monkeypatch):
    import ops_dashboard.db as _db
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "an.db")
    _db.init_db()
    return _db


def test_same_alert_is_logged_once_per_day(feed_db):
    alert = {"severity": "high", "message": "URL x unhealthy: HTTP 404", "project": "p"}
    for _ in range(5):                       # five dashboard loads
        anomalies._log_to_feed([alert])
    anomalies._log_to_feed([{**alert, "message": "URL y unhealthy: HTTP 500"}])
    with feed_db.db() as conn:
        rows = conn.execute(
            "SELECT summary FROM activity_feed WHERE kind = 'anomaly_detected' ORDER BY id"
        ).fetchall()
    assert [r[0] for r in rows] == ["URL x unhealthy: HTTP 404", "URL y unhealthy: HTTP 500"]
