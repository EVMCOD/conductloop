"""Google Calendar connector (read + ingest). No network: recorded Calendar API
responses behind ProviderFake; the only allowed sockets are loopback."""
from __future__ import annotations

import socket

import httpx
import pytest
from connectors_native_fake import NetworkForbidden, ProviderFake, connect, recorded, sqlite_text

from ops_dashboard import connector_actions as actions
from ops_dashboard import connector_oauth as oauth
from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens
from ops_dashboard.connectors_native import google_calendar as gcal

REC = recorded("google_calendar")
API = "www.googleapis.com/calendar/v3"
SECRET = "GOCSPX-fake-desktop-secret"


@pytest.fixture(autouse=True)
def _no_network():
    real = socket.socket.connect

    def guarded(self, address):
        if (address[0] if isinstance(address, tuple) else address) not in ("127.0.0.1", "::1", "localhost"):
            raise NetworkForbidden(f"network access in tests: {address!r}")
        return real(self, address)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", guarded)
        yield


def _events(request: httpx.Request) -> httpx.Response:
    q = request.url.params
    assert q["singleEvents"] == "true" and q["timeMin"].endswith("Z") and q["timeMax"].endswith("Z")
    if "holiday" in request.url.path:
        return httpx.Response(200, json=REC["events_holidays"])
    return httpx.Response(200, json=REC["events_primary_page2" if q.get("pageToken") == "p2" else "events_primary_page1"])


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gcal.db")
    db.init_db()
    fake = ProviderFake(
        token_url="https://oauth2.googleapis.com/token",
        hosts=("oauth2.googleapis.com", "accounts.google.com", "www.googleapis.com"),
        client_secret=SECRET,
        routes={
            ("GET", f"{API}/users/me/calendarList"): REC["calendarList"],
            ("GET", f"{API}/calendars/ana%40example.com/events"): _events,
            ("GET", f"{API}/calendars/es.spain%23holiday%40group.v.calendar.google.com/events"): _events,
        })
    prev_t = oauth.set_transport(fake.transport())
    prev_b = tokens.use_backend(tokens.MemoryBackend())
    try:
        yield fake, db
    finally:
        tokens.use_backend(prev_b)
        oauth.set_transport(prev_t)


def test_registered_from_its_own_module_with_minimal_read_scopes():
    spec = registry.get("google_calendar")
    assert spec is gcal.SPEC and spec.auth == "oauth_pkce" and spec.client_secret_required
    assert spec.read_scopes == ("https://www.googleapis.com/auth/calendar.calendarlist.readonly",
                                "https://www.googleapis.com/auth/calendar.events.readonly")
    assert spec.write_scopes == ("https://www.googleapis.com/auth/calendar.events.owned",)
    assert {n for n, a in spec.actions.items() if a.kind == "read"} == {"list_calendars", "list_events", "sync"}
    assert spec.revoke_url == ""          # revoking would kill every token of the user's Google project
    assert set(spec.allowed_hosts) == {"accounts.google.com", "oauth2.googleapis.com", "www.googleapis.com"}
    assert spec.public()["byok_required"] is True


def test_guide_and_spec_agree_on_scopes_and_redirect():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "ops_dashboard" / "web" / "connectors-guide.js").read_text()
    for s in gcal.READ_SCOPES:
        assert f"scope: '{s}', kind: 'read'" in js


def test_connect_requests_only_read_scopes_on_loopback(env):
    fake, _db = env
    params = connect("google_calendar", fake, client_secret=SECRET)
    assert params["_authorize_base"] == "https://accounts.google.com/o/oauth2/v2/auth"
    assert params["scope"].split() == list(gcal.READ_SCOPES)
    assert params["redirect_uri"].startswith("http://127.0.0.1:") and params["redirect_uri"].endswith("/callback")
    assert params["code_challenge_method"] == "S256" and params["client_id"] == "byok-client"
    acc = store.get_account("google_calendar")
    assert acc["status"] == "connected" and acc["permission"] == "read"


def test_needs_client_secret_byok(env):
    store.upsert_account("google_calendar", client_id="byok-client")
    with pytest.raises(oauth.OAuthError) as e:
        oauth.start("google_calendar", "read", listen=False)
    assert e.value.code == "client_secret_required"


def test_sync_ingests_agenda_and_vault_and_prunes(env, monkeypatch):
    fake, db = env
    monkeypatch.setattr(gcal.time, "time", lambda: 1792000000.0)   # 2026-10-14…; window covers fixtures
    connect("google_calendar", fake, client_secret=SECRET)
    out = actions.invoke("google_calendar", "sync", {"days_ahead": 60})
    st = out["result"]
    assert out["status"] == "done"
    assert st == {"calendars": 2, "events": 3, "vault_updated": 3, "pruned": 0, "truncated": 0}
    with db.db() as conn:
        rows = conn.execute("SELECT uid, title, all_day, source FROM calendar_events ORDER BY starts_at").fetchall()
        docs = conn.execute("SELECT path, title, content FROM documents WHERE source='google_calendar'").fetchall()
    assert [r[1] for r in rows] == ["Revisión trimestral", "Dentista", "Día de Todos los Santos"]
    assert rows[2][2] == 1 and {r[3] for r in rows} == {"google_calendar"}
    assert all(r[0].startswith("gcal:") for r in rows)
    rev = next(d for d in docs if d[1] == "Revisión trimestral")
    assert "Sala 2" in rev[2] and "Asistentes: 2" in rev[2] and "luis@example.com" not in rev[2]
    assert "Cancelada" not in {d[1] for d in docs}
    assert store.get_account("google_calendar")["account_label"] == "ana@example.com"
    assert "hidden@group" not in str([str(r.url) for r in fake.requests])     # unselected calendar not read

    # Re-sync is idempotent; an event that vanished upstream is pruned from the agenda.
    REC["events_primary_page2"]["items"] = []
    try:
        st2 = actions.invoke("google_calendar", "sync", {"days_ahead": 60})["result"]
    finally:
        REC["events_primary_page2"]["items"] = recorded("google_calendar")["events_primary_page2"]["items"]
    # One row from the agenda and its document from the vault (Codex on #49).
    assert st2["vault_updated"] == 0 and st2["pruned"] == 2
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM documents WHERE source='google_calendar'").fetchone()[0] == 2


def test_truncated_listing_never_prunes(env, monkeypatch):
    fake, db = env
    monkeypatch.setattr(gcal.time, "time", lambda: 1792000000.0)
    connect("google_calendar", fake, client_secret=SECRET)
    actions.invoke("google_calendar", "sync", {"days_ahead": 60})
    real = gcal._google.paged_ex                              # primary has 2 pages: read only 1
    monkeypatch.setattr(gcal._google, "paged_ex", lambda *a, **k: real(*a, **{**k, "max_pages": 1}))
    REC["events_primary_page2"]["items"] = []
    try:
        st = actions.invoke("google_calendar", "sync", {"days_ahead": 60})["result"]
    finally:
        REC["events_primary_page2"]["items"] = recorded("google_calendar")["events_primary_page2"]["items"]
    assert st["truncated"] == 1 and st["pruned"] == 0
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == 3


def test_patch_can_clear_location_and_description():
    body = gcal._event_body({"event_id": "abc", "location": "", "description": ""}, partial=True)
    assert body == {"location": "", "description": ""}


def test_tokens_never_in_sqlite_and_only_google_hosts(env, monkeypatch, tmp_path):
    fake, db = env
    monkeypatch.setattr(gcal.time, "time", lambda: 1792000000.0)
    connect("google_calendar", fake, client_secret=SECRET)
    actions.invoke("google_calendar", "sync", {})
    dump = sqlite_text(db.DB_PATH)
    assert "fake-at-" not in dump and "fake-rt" not in dump and SECRET not in dump
    assert {r.url.host for r in fake.requests} <= {"oauth2.googleapis.com", "www.googleapis.com"}
    events = [e["event"] for e in store.list_audit(50, "google_calendar")]
    assert "connected" in events and "read" in events


def test_list_actions_and_api_error_is_reported_not_leaked(env, monkeypatch):
    fake, _db = env
    connect("google_calendar", fake, client_secret=SECRET)
    cals = actions.invoke("google_calendar", "list_calendars")["result"]["calendars"]
    assert [c["id"] for c in cals if c["selected"]] == ["ana@example.com", "es.spain#holiday@group.v.calendar.google.com"]
    fake.routes[("GET", f"{API}/users/me/calendarList")] = lambda r: httpx.Response(
        403, json={"error": {"status": "PERMISSION_DENIED", "message": "nope"}})
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("google_calendar", "sync", {})
    assert e.value.code == "action_failed"
    assert [x["event"] for x in store.list_audit(5, "google_calendar")][0] == "read_failed"


def test_sync_route_is_local_only_and_runs_sync(env, monkeypatch):
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from ops_dashboard.server import DashboardHandler
    fake, _db = env
    monkeypatch.setattr(gcal.time, "time", lambda: 1792000000.0)
    connect("google_calendar", fake, client_secret=SECRET)

    class _Q(DashboardHandler):
        def log_message(self, *_a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        def post(path, headers=None):
            req = urllib.request.Request(base + path, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")
        code, body = post("/api/connectors-sync/google_calendar")
        assert code == 200 and body["result"]["events"] == 3
        assert post("/api/connectors-sync/google_calendar", {"Origin": "https://evil.example"})[0] == 403
        assert post("/api/connectors-sync/nope")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()
