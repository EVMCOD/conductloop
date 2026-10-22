"""Microsoft 365 connector (Outlook mail + calendar, read + ingest). No
network: recorded Microsoft Graph responses behind ProviderFake."""
from __future__ import annotations

import json
import socket

import httpx
import pytest
from connectors_native_fake import NetworkForbidden, ProviderFake, connect, recorded, sqlite_text

from ops_dashboard import connector_actions as actions
from ops_dashboard import connector_oauth as oauth
from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens
from ops_dashboard.connectors_native import microsoft365 as m365

REC = recorded("microsoft365")
G = "graph.microsoft.com/v1.0"
NOW = 1792001000.0         # 2026-10-14


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


def _inbox(request: httpx.Request) -> httpx.Response:
    q = request.url.params
    if q.get("$skip") == "1":
        return httpx.Response(200, json=REC["inbox_page2"])
    if "body" in q.get("$select", ""):
        assert request.headers["Prefer"] == 'outlook.body-content-type="text"'
        assert q["$filter"].startswith("receivedDateTime ge 2026-10-")
    return httpx.Response(200, json=REC["inbox_page1"])


def _calendar(request: httpx.Request) -> httpx.Response:
    q = request.url.params
    assert q["startDateTime"].endswith("Z") and q["endDateTime"].endswith("Z")
    assert request.headers["Prefer"] == 'outlook.timezone="UTC"'
    return httpx.Response(200, json=REC["calendar"])


def make_fake() -> ProviderFake:
    return ProviderFake(
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        hosts=("login.microsoftonline.com", "graph.microsoft.com"),
        routes={("GET", f"{G}/me"): REC["me"], ("GET", f"{G}/me/mailFolders/inbox/messages"): _inbox,
                ("GET", f"{G}/me/messages/AAMkAD1=="): REC["inbox_page1"]["value"][0],
                ("GET", f"{G}/me/calendarView"): _calendar})


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "m365.db")
    monkeypatch.setattr(m365.time, "time", lambda: NOW)
    db.init_db()
    fake = make_fake()
    prev_t, prev_b = oauth.set_transport(fake.transport()), tokens.use_backend(tokens.MemoryBackend())
    try:
        yield fake, db
    finally:
        tokens.use_backend(prev_b)
        oauth.set_transport(prev_t)


def test_spec_public_client_minimal_read_scopes():
    spec = registry.get("microsoft365")
    assert spec is m365.SPEC and spec.auth == "oauth_pkce" and not spec.client_secret_required
    assert spec.read_scopes == ("offline_access", "https://graph.microsoft.com/User.Read",
                                "https://graph.microsoft.com/Mail.Read", "https://graph.microsoft.com/Calendars.Read")
    assert spec.write_scopes == ("https://graph.microsoft.com/Mail.Send", "https://graph.microsoft.com/Calendars.ReadWrite")
    assert set(spec.allowed_hosts) == {"login.microsoftonline.com", "graph.microsoft.com"}
    assert spec.authorize_url == "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    assert (spec.redirect_host, spec.redirect_port) == ("localhost", 0)


def test_connect_pkce_localhost_no_secret(env):
    fake, _ = env
    params = connect("microsoft365", fake)
    assert params["redirect_uri"].startswith("http://localhost:") and params["redirect_uri"].endswith("/callback")
    assert params["code_challenge_method"] == "S256" and params["response_mode"] == "query"
    assert "Mail.Send" not in params["scope"] and "ReadWrite" not in params["scope"]
    tok = [r for r in fake.requests if r.url.host == "login.microsoftonline.com"][0]
    assert "Authorization" not in tok.headers and b"client_secret" not in tok.content


def test_sync_mail_and_calendar_idempotent(env):
    fake, db = env
    connect("microsoft365", fake)
    st = actions.invoke("microsoft365", "sync", {})["result"]
    assert st == {"messages": 2, "events": 2, "vault_updated": 4, "pruned": 0}
    again = actions.invoke("microsoft365", "sync", {})["result"]
    assert again["vault_updated"] == 0
    with db.db() as c:
        docs = {r["path"]: r for r in c.execute("SELECT * FROM documents WHERE source='microsoft365'")}
        agenda = [tuple(r) for r in c.execute("SELECT uid, title, all_day FROM calendar_events ORDER BY starts_at")]
    mail = docs["microsoft365/mail/AAMkAD1__"]["content"]
    assert "- De: Luis <luis@contoso.example>" in mail and "Adjuntos: sí (no descargados)" in mail
    assert json.loads(docs["microsoft365/mail/AAMkAD1__"]["metadata"])["untrusted"] is True
    news = docs["microsoft365/mail/AAMkAD2__"]["content"]
    assert "Novedades" in news and "alert" not in news and "<p>" not in news
    assert agenda == [("m365:AAMkEV1_", "Comité", 0), ("m365:AAMkEV3_", "Puente", 1)]
    ev = docs["microsoft365/event/AAMkEV1_"]["content"]
    assert "Asistentes: 1" in ev and "luis@contoso.example" not in ev
    assert store.get_account("microsoft365")["account_label"] == "ana@contoso.example"
    assert all(r.method == "GET" for r in fake.requests if r.url.host == "graph.microsoft.com")
    assert actions.list_actions("pending") == []          # injected mail text queued nothing


def test_vanished_event_is_pruned(env):
    fake, db = env
    connect("microsoft365", fake)
    actions.invoke("microsoft365", "sync", {"mail": False})
    fake.routes[("GET", f"{G}/me/calendarView")] = lambda r: httpx.Response(200, json={"value": REC["calendar"]["value"][:1]})
    assert actions.invoke("microsoft365", "sync", {"mail": False})["result"]["pruned"] == 1


def test_get_message_list_events_and_bad_ids(env):
    fake, _ = env
    connect("microsoft365", fake)
    msg = actions.invoke("microsoft365", "get_message", {"id": "AAMkAD1=="})["result"]
    assert msg["subject"] == "Presupuesto 2027" and msg["text"].startswith("Hola Ana")
    evs = actions.invoke("microsoft365", "list_events", {"days_back": 1, "days_ahead": 30})["result"]["events"]
    assert [e["title"] for e in evs] == ["Comité", "Puente"]
    with pytest.raises(actions.ActionError):
        actions.invoke("microsoft365", "get_message", {"id": "../../me/sendMail"})


def test_next_link_must_stay_on_graph(env):
    fake, _ = env
    connect("microsoft365", fake)
    evil = dict(REC["inbox_page1"], **{"@odata.nextLink": "https://evil.example/steal"})
    fake.routes[("GET", f"{G}/me/mailFolders/inbox/messages")] = lambda r: httpx.Response(200, json=evil)
    with pytest.raises(actions.ActionError):
        actions.invoke("microsoft365", "sync", {"calendar": False})
    assert not [r for r in fake.requests if r.url.host == "evil.example"]


def test_graph_error_and_tokens_not_in_sqlite(env):
    fake, db = env
    connect("microsoft365", fake)
    actions.invoke("microsoft365", "sync", {})
    assert "fake-at-" not in sqlite_text(db.DB_PATH) and "fake-rt" not in sqlite_text(db.DB_PATH)
    fake.routes[("GET", f"{G}/me")] = lambda r: httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}})
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("microsoft365", "sync", {})
    assert e.value.code == "action_failed"
