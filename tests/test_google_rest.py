"""Google Analytics, Search Console and Google Play without Google's libraries.

The packaged app does not ship google-analytics-data or google-auth, so every
GA and Search Console poll in it failed with "No module named
'google.analytics'" / "'google.auth'" from the day people moved to the app
(22 Oct 2026). These tests pin the REST replacement (ops_dashboard.google_oauth
plus plain HTTPS calls), the backfill after an outage, and the anomaly check
that stopped comparing a stale, half-synced day with the week before it.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import pathlib
import re

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

AUTHORIZED_USER = {
    "client_id": "cid.apps.googleusercontent.com",
    "client_secret": "csecret",
    "refresh_token": "1//refresh",
    "token_uri": "https://oauth2.googleapis.com/token",
    "token": "expired-access-token",
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    from ops_dashboard import google_oauth
    google_oauth.clear_cache()
    yield
    google_oauth.clear_cache()


class _Recorder:
    """Stands in for httpx.post/get: records calls and answers from a table."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        for pattern, response in self.answers:
            if re.search(pattern, url):
                return response(url, kw) if callable(response) else response
        raise AssertionError(f"unexpected call to {url}")


def _token_ok(token="ya29.fresh", expires_in=3600):
    return httpx.Response(200, json={"access_token": token, "expires_in": expires_in, "token_type": "Bearer"})


# ── google_oauth ─────────────────────────────────────────────────────────────

def test_a_stored_sign_in_is_refreshed_with_a_plain_post(monkeypatch):
    from ops_dashboard import google_oauth
    rec = _Recorder([(r"oauth2\.googleapis\.com/token", _token_ok())])
    monkeypatch.setattr(httpx, "post", rec)

    assert google_oauth.access_token(json.dumps(AUTHORIZED_USER), ["s1"]) == "ya29.fresh"
    url, kw = rec.calls[0]
    assert url == "https://oauth2.googleapis.com/token"
    assert kw["data"] == {"grant_type": "refresh_token", "refresh_token": "1//refresh",
                          "client_id": "cid.apps.googleusercontent.com", "client_secret": "csecret"}
    # Cached: seven sites polled in a row refresh once.
    google_oauth.access_token(AUTHORIZED_USER, ["s1"])
    assert len(rec.calls) == 1


def test_a_revoked_sign_in_says_to_connect_again(monkeypatch):
    from ops_dashboard import google_oauth
    rec = _Recorder([(r"/token", httpx.Response(400, json={
        "error": "invalid_grant", "error_description": "Token has been expired or revoked."}))])
    monkeypatch.setattr(httpx, "post", rec)
    with pytest.raises(google_oauth.GoogleAuthError, match="connect the Google account again"):
        google_oauth.access_token(AUTHORIZED_USER, ["s1"])


def test_a_service_account_signs_its_own_jwt(monkeypatch):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    from ops_dashboard import google_oauth
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    sa = {"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com",
          "private_key": pem, "private_key_id": "kid1"}
    rec = _Recorder([(r"/token", _token_ok("ya29.sa"))])
    monkeypatch.setattr(httpx, "post", rec)

    assert google_oauth.access_token(sa, ["https://www.googleapis.com/auth/analytics.readonly"]) == "ya29.sa"
    data = rec.calls[0][1]["data"]
    assert data["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    head, claims, sig = data["assertion"].split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    c = json.loads(base64.urlsafe_b64decode(pad(claims)))
    assert c["iss"] == sa["client_email"] and c["aud"] == google_oauth.TOKEN_URI
    assert c["scope"] == "https://www.googleapis.com/auth/analytics.readonly"
    assert json.loads(base64.urlsafe_b64decode(pad(head)))["alg"] == "RS256"
    key.public_key().verify(base64.urlsafe_b64decode(pad(sig)), f"{head}.{claims}".encode(),
                            padding.PKCS1v15(), hashes.SHA256())


def test_nothing_imports_googles_client_libraries():
    """The packaged app does not ship them: an import is a poll that always fails."""
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "ops_dashboard").rglob("*.py")
                 if re.search(r"^\s*(from|import)\s+google\b", p.read_text(encoding="utf-8"), re.M)]
    assert offenders == []


# ── GA4 over REST ────────────────────────────────────────────────────────────

def _row(date, channel, country, users, sessions):
    return {"dimensionValues": [{"value": date}, {"value": channel}, {"value": country}],
            "metricValues": [{"value": str(v)} for v in (users, users // 2, sessions, sessions * 2, 10,
                                                          "0.5", "60.0", "0.4")]}


def test_ga4_reads_the_report_over_https(monkeypatch):
    from ops_dashboard.indexers import ga
    report = {"rows": [_row("20261113", "Organic Search", "Spain", 30, 40),
                       _row("20261113", "Direct", "Mexico", 10, 10),
                       _row("20261114", "Referral", "Spain", 5, 5)]}
    rec = _Recorder([(r"/token", _token_ok()),
                     (r"analyticsdata\.googleapis\.com/v1beta/properties/123:runReport",
                      httpx.Response(200, json=report))])
    monkeypatch.setattr(httpx, "post", rec)
    src = ga.GA4Source({"id": 1, "property_id": "123", "project_key": "acme",
                        "service_account_json": json.dumps(AUTHORIZED_USER)}, {})

    days = {d.date: d for d in src.fetch_metrics(days=7)}
    assert set(days) == {"2026-11-13", "2026-11-14"}
    d = days["2026-11-13"]
    assert (d.users, d.source_organic, d.source_direct, d.top_country) == (40, 30, 10, "Spain")
    url, kw = rec.calls[1]
    assert kw["headers"]["Authorization"] == "Bearer ya29.fresh"
    assert kw["json"]["dateRanges"] == [{"startDate": "7daysAgo", "endDate": "today"}]


def test_ga4_test_reads_the_property_metadata(monkeypatch):
    from ops_dashboard.indexers import ga
    monkeypatch.setattr(httpx, "post", _Recorder([(r"/token", _token_ok())]))
    monkeypatch.setattr(httpx, "get", _Recorder([(r"/properties/123/metadata", httpx.Response(
        200, json={"dimensions": [{}, {}], "metrics": [{}]}))]))
    src = ga.GA4Source({"id": 1, "property_id": "properties/123", "project_key": "acme",
                        "service_account_json": json.dumps(AUTHORIZED_USER)}, {})
    ok, msg = src.test()
    assert ok and "2 dimensions, 1 metrics" in msg


def test_a_ga4_api_error_carries_googles_reason(monkeypatch):
    from ops_dashboard.indexers import ga
    monkeypatch.setattr(httpx, "post", _Recorder([
        (r"/token", _token_ok()),
        (r":runReport", httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED",
                                                            "message": "User does not have sufficient permissions"}}))]))
    src = ga.GA4Source({"id": 1, "property_id": "123", "project_key": "acme",
                        "service_account_json": json.dumps(AUTHORIZED_USER)}, {})
    with pytest.raises(RuntimeError, match="403: PERMISSION_DENIED User does not have"):
        src.fetch_metrics(days=7)


def test_search_console_queries_with_a_bearer_token(monkeypatch):
    from ops_dashboard.indexers import gsc
    rec = _Recorder([(r"/token", _token_ok()),
                     (r"searchconsole\.googleapis\.com/webmasters/v3/sites/sc-domain%3Aacme\.example/searchAnalytics/query",
                      lambda url, kw: httpx.Response(200, json={"rows": []},
                                                     request=httpx.Request("POST", url)))])
    monkeypatch.setattr(httpx, "post", rec)
    src = gsc.GSCSource({"site_url": "sc-domain:acme.example", "credentials_json": json.dumps(AUTHORIZED_USER)})
    assert src._query({"startDate": "2026-11-01", "endDate": "2026-11-07"}) == {"rows": []}
    assert rec.calls[1][1]["headers"]["Authorization"] == "Bearer ya29.fresh"


# ── backfill and stale data ──────────────────────────────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "a.db"))
    from ops_dashboard import brain, db, i18n
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "a.db")
    monkeypatch.setattr(brain, "CONFIG_PATH", tmp_path / "config.json")
    db.init_db()
    i18n.set_locale("es")
    return db


def _store(db, site_id, start, values):
    for i, users in enumerate(values):
        day = (start + dt.timedelta(days=i)).isoformat()
        db.upsert_ga_metrics_daily(site_id=site_id, property_id="properties/1", project_key="acme",
                                   date=day, users=users)


def test_after_an_outage_the_poll_reaches_back_to_the_last_stored_day(env):
    from ops_dashboard.indexers import ga
    site = env.add_ga_site("properties/1", "Acme", "acme", service_account_json="{}")
    today = dt.date(2026, 11, 14)
    assert ga._days_to_fetch(site, 7, today) == 7  # nothing stored yet
    _store(env, site, dt.date(2026, 10, 16), [210, 220, 230, 215, 225, 224, 66])  # last: 22 Oct
    assert ga._days_to_fetch(site, 7, today) == 23
    assert ga._days_to_fetch(site, 7, dt.date(2027, 6, 1)) == ga.MAX_BACKFILL_DAYS


def _yesterday():
    return min(dt.date.today(), dt.datetime.now(dt.UTC).date()) - dt.timedelta(days=1)


def test_a_stale_half_synced_day_is_not_a_drop_but_a_stopped_sync(env):
    """The case on screen for weeks: 66 users on 22 Oct vs ~227, every day."""
    from ops_dashboard.indexers import anomalies
    site = env.add_ga_site("properties/1", "Acme", "acme", service_account_json="{}")
    env.record_ga_site_poll(site, "error", "No module named 'google.analytics'")
    last = _yesterday() - dt.timedelta(days=23)
    _store(env, site, last - dt.timedelta(days=8), [196, 210, 325, 218, 213, 207, 224, 215, 66])

    found = anomalies.detect_recent_anomalies()["anomalies"]
    ga = [a for a in found if str(a.get("kind", "")).startswith("ga_")]
    assert [a["kind"] for a in ga] == ["ga_stale"]
    assert ga[0]["message"] == (f"Google Analytics no se actualiza desde el {last.isoformat()} "
                                "(último error: No module named 'google.analytics')")


def test_fresh_data_still_raises_a_real_drop(env):
    """A drop on a settled day (the day before yesterday) is still an alert."""
    from ops_dashboard.indexers import anomalies
    site = env.add_ga_site("properties/1", "Acme", "acme", service_account_json="{}")
    _store(env, site, _yesterday() - dt.timedelta(days=9), [210, 220, 230, 215, 225, 224, 218, 222, 40, 230])

    found = {a["kind"]: a for a in anomalies.detect_recent_anomalies()["anomalies"]}
    assert "ga_users_total" in found and "ga_stale" not in found
    assert found["ga_users_total"]["date"] == (_yesterday() - dt.timedelta(days=1)).isoformat()


def test_yesterday_is_not_compared_while_ga_is_still_closing_it(env):
    """GA4 closes a day 24-48 h late: a short yesterday is not a drop."""
    from ops_dashboard.indexers import anomalies
    site = env.add_ga_site("properties/1", "Acme", "acme", service_account_json="{}")
    _store(env, site, _yesterday() - dt.timedelta(days=8), [210, 220, 230, 215, 225, 224, 218, 222, 40])

    kinds = [a["kind"] for a in anomalies.detect_recent_anomalies()["anomalies"]]
    assert not [k for k in kinds if k.startswith("ga_")]
