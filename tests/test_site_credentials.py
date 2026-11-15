"""The Google credentials of a site must not sit in the database in clear text.

They did: ``ga_sites.service_account_json`` and ``gsc_sites.credentials_json``
each held an OAuth *authorized_user* blob with a refresh token — 14 of them on
the owner's machine (7 GA properties, 7 Search Console sites). A refresh token
is enough to act as the Google account behind it.

They now live in the secrets store (the keychain, or a 0600 file when
``OPS_DISABLE_KEYCHAIN=1``) and the column keeps only the reference.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import httpx
import pytest

BLOB = {
    "client_id": "cid.apps.googleusercontent.com",
    "client_secret": "csecret",
    "refresh_token": "1//refresh",
    "token_uri": "https://oauth2.googleapis.com/token",
}
BLOB_JSON = json.dumps(BLOB)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "a.db"))
    # A secrets store of its own: the sandbox of the session is shared by every
    # test, and these secrets are named after the site id.
    monkeypatch.setenv("OPS_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("OPS_DISABLE_KEYCHAIN", "1")
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "a.db")
    db.init_db()
    return db


def _run_migration_43(env) -> None:
    """Run the migration on a database that pretends to be one version behind."""
    from ops_dashboard import migrations
    with env.get_connection() as conn:
        conn.execute("PRAGMA user_version = 42")
        migrations.run_migrations(conn)


def _token_ok(token="ya29.fresh", expires_in=3600):
    return httpx.Response(200, json={"access_token": token, "expires_in": expires_in, "token_type": "Bearer"})


class _Recorder:
    """Stands in for httpx.post: records calls and answers from a table."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        for pattern, response in self.answers:
            if pattern in url:
                return response
        raise AssertionError(f"unexpected call to {url}")


# ── the migration ─────────────────────────────────────────────────────────────

def test_the_migration_takes_the_credentials_out_of_the_database(env):
    from ops_dashboard import site_credentials

    site_id = env.add_ga_site("properties/1", "Acme", "acme", service_account_json=BLOB_JSON)
    gsc_id = env.add_gsc_site("sc-domain:acme.example", "Acme", "acme", credentials_json=BLOB_JSON)
    assert env.get_ga_site(site_id)["service_account_json"] == BLOB_JSON

    _run_migration_43(env)

    assert env.get_ga_site(site_id)["service_account_json"] is None
    assert env.list_gsc_sites()[0]["credentials_json"] is None
    # ... and they are in the secrets store, where the indexers can find them.
    assert site_credentials.get_ga_credentials(env.get_ga_site(site_id)) == BLOB_JSON
    assert site_credentials.get_gsc_credentials(env.list_gsc_sites()[0]) == BLOB_JSON
    assert gsc_id


def test_after_the_migration_no_row_keeps_a_token(env):
    env.add_ga_site("properties/1", "Acme", "acme", service_account_json=BLOB_JSON)
    env.add_gsc_site("sc-domain:acme.example", "Acme", "acme", credentials_json=BLOB_JSON)

    _run_migration_43(env)

    with env.get_connection() as conn:
        ga_values = [r[0] for r in conn.execute("SELECT service_account_json FROM ga_sites")]
        gsc_values = [r[0] for r in conn.execute("SELECT credentials_json FROM gsc_sites")]
    for value in ga_values + gsc_values:
        assert not value or ("refresh_token" not in value and "private_key" not in value)


def test_the_migration_keeps_the_column_when_the_secrets_store_refuses(env, monkeypatch):
    """Losing a credential is worse than keeping it where it was."""
    from ops_dashboard import secrets_store

    env.add_ga_site("properties/1", "Acme", "acme", service_account_json=BLOB_JSON)

    def _boom(name, value):
        raise RuntimeError("the keychain is locked")

    monkeypatch.setattr(secrets_store, "set_secret", _boom)
    monkeypatch.setattr("ops_dashboard.site_credentials.set_ga_credentials",
                        lambda site_id, blob: _boom(site_id, blob))

    _run_migration_43(env)

    assert env.get_ga_site(1)["service_account_json"] == BLOB_JSON


def test_the_migration_is_idempotent(env):
    env.add_ga_site("properties/1", "Acme", "acme", service_account_json=BLOB_JSON)
    _run_migration_43(env)
    _run_migration_43(env)  # a second run has nothing to move
    assert env.get_ga_site(1)["service_account_json"] is None


# ── the polls still work ──────────────────────────────────────────────────────

def test_the_ga_poll_reads_the_credential_from_the_secrets_store(env, monkeypatch):
    from ops_dashboard import site_credentials
    from ops_dashboard.indexers import ga

    site_id = env.add_ga_site("properties/1", "Acme", "acme", source_type="ga4")
    site_credentials.set_ga_credentials(site_id, BLOB_JSON)
    assert env.get_ga_site(site_id)["service_account_json"] is None

    report = {"rows": [{"dimensionValues": [{"value": "20261114"}, {"value": "Organic Search"},
                                             {"value": "Spain"}],
                        "metricValues": [{"value": str(v)} for v in (30, 15, 40, 80, 10, "0.5", "60.0", "0.4")]}]}
    rec = _Recorder([("/token", _token_ok()),
                     (":runReport", httpx.Response(200, json=report))])
    monkeypatch.setattr(httpx, "post", rec)

    src = ga.GA4Source(env.get_ga_site(site_id), {})
    days = {d.date: d for d in src.fetch_metrics(days=7)}
    assert days["2026-11-14"].users == 30
    assert rec.calls[1][1]["headers"]["Authorization"] == "Bearer ya29.fresh"


def test_the_search_console_poll_reads_the_credential_from_the_secrets_store(env, monkeypatch):
    from ops_dashboard import site_credentials
    from ops_dashboard.indexers import gsc

    gsc_id = env.add_gsc_site("sc-domain:acme.example", "Acme", "acme")
    site_credentials.set_gsc_credentials(gsc_id, BLOB_JSON)
    row = env.list_gsc_sites()[0]
    assert row["credentials_json"] is None

    rec = _Recorder([("/token", _token_ok()),
                     ("searchAnalytics/query", httpx.Response(
                         200, json={"rows": []},
                         request=httpx.Request("POST", "https://searchconsole.googleapis.com/")))])
    monkeypatch.setattr(httpx, "post", rec)

    src = gsc.GSCSource(row)
    assert src._query({"startDate": "2026-11-01", "endDate": "2026-11-07"}) == {"rows": []}
    assert rec.calls[1][1]["headers"]["Authorization"] == "Bearer ya29.fresh"


def test_a_database_that_has_not_been_migrated_still_works(env, monkeypatch):
    """The column stays the fallback, so an old database keeps polling."""
    from ops_dashboard.indexers import ga

    site_id = env.add_ga_site("properties/1", "Acme", "acme", service_account_json=BLOB_JSON)
    rec = _Recorder([("/token", _token_ok()),
                     (":runReport", httpx.Response(200, json={"rows": []}))])
    monkeypatch.setattr(httpx, "post", rec)

    src = ga.GA4Source(env.get_ga_site(site_id), {})
    assert src.sa_json == BLOB_JSON
    assert src.fetch_metrics(days=7) == []


def test_a_site_with_only_a_stored_credential_is_not_taken_for_a_mock(env):
    """The credential moved out of the column: the site is still real GA4."""
    from ops_dashboard import site_credentials
    from ops_dashboard.indexers import ga

    site_id = env.add_ga_site("properties/1", "Acme", "acme")
    site_credentials.set_ga_credentials(site_id, BLOB_JSON)
    # A row without an explicit type: the credential decides, and it now lives
    # in the secrets store instead of the column.
    with env.get_connection() as conn:
        conn.execute("UPDATE ga_sites SET source_type = NULL WHERE id = ?", (site_id,))
    row = env.get_ga_site(site_id)
    assert row["service_account_json"] is None
    assert row["source_type"] is None

    assert isinstance(ga.get_source(row), ga.GA4Source)


def test_a_site_without_any_credential_is_still_a_mock(env):
    from ops_dashboard.indexers import ga

    site_id = env.add_ga_site("properties/1", "Acme", "acme")
    assert isinstance(ga.get_source(env.get_ga_site(site_id)), ga.MockSource)


# ── the API ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def server(env):
    import ops_dashboard.server as server_mod
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.DashboardHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port
    httpd.shutdown()
    httpd.server_close()


def _post(port: int, path: str, body: dict):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _get(port: int, path: str):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_adding_a_site_stores_the_credential_in_the_secrets_store(env, server):
    from ops_dashboard import site_credentials

    status, data = _post(server, "/api/ga/sites", {
        "property_id": "properties/1", "display_name": "Acme", "project_key": "acme",
        "service_account_json": BLOB_JSON,
    })
    assert status == 200 and data["ok"]
    site_id = data["id"]

    # Not in the database...
    assert env.get_ga_site(site_id)["service_account_json"] is None
    # ... in the secrets store.
    assert site_credentials.get_ga_credentials(env.get_ga_site(site_id)) == BLOB_JSON


def test_the_api_does_not_hand_out_the_credential(env, server):
    status, data = _post(server, "/api/ga/sites", {
        "property_id": "properties/1", "display_name": "Acme", "project_key": "acme",
        "service_account_json": BLOB_JSON,
    })
    assert status == 200
    site_id = data["id"]

    status, row = _get(server, f"/api/ga/sites/{site_id}")
    assert status == 200
    assert "service_account_json" not in row
    assert BLOB_JSON not in json.dumps(row)


def test_deleting_a_site_takes_its_credential_with_it(env, server):
    from ops_dashboard import secrets_store, site_credentials

    status, data = _post(server, "/api/ga/sites", {
        "property_id": "properties/1", "display_name": "Acme", "project_key": "acme",
        "service_account_json": BLOB_JSON,
    })
    assert status == 200
    site_id = data["id"]

    req = urllib.request.Request(f"http://127.0.0.1:{server}/api/ga/sites/{site_id}", method="DELETE")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200

    assert secrets_store.get_secret(site_credentials.ga_secret_name(site_id)) is None
