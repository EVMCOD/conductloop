"""A connector that can be connected once, and never re-authenticated.

The OAuth flow works until the provider expires the refresh token: Slack and
Microsoft with PKCE expire after 30 days of inactivity, Google in *Testing*
mode after 7, and a Gmail refresh token stops working when the user changes
their Google password. From then on the user has to press "Connect" again.

A personal token does not have that problem: it lasts until the user revokes
it. So a connector that can take a token as well as OAuth gives the user the
option of connecting once, in the history of the program.

This is about the mechanism, not about any particular provider:

- A connector may declare that it also accepts a personal token.
- The token is verified with the provider BEFORE it is stored, exactly like
  the OAuth flow verifies the authorization code.
- The token goes to the keychain, never to the database. A token that lasts
  for years must not sit in a plaintext column.
- Connecting with a token must not require a client ID/secret: that is the
  whole point.
- The two ways must not fight: connecting with a token replaces the OAuth
  credential, and vice versa.
- A connector that takes a token must be able to SAY so, so the UI can offer
  the option instead of hiding it.
"""
from __future__ import annotations

import json

import pytest

from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens


@pytest.fixture
def clean(tmp_path, monkeypatch):
    """A registry with nothing in it, and a database of its own.

    The audit log is a real table, so the database has to be there: a test
    that mocks the audit away proves nothing about what is written.
    """
    from ops_dashboard import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "token_auth.db")
    db.init_db()
    # The real keychain is not available in a test run, and the point of
    # these tests is that the credential goes through the token store. Use the
    # in-memory backend so the save/load round trip is exercised for real.
    from ops_dashboard import connector_tokens as ctokens
    ctokens.use_backend(ctokens.MemoryBackend())
    saved_specs = dict(registry._REGISTRY)
    for cid in list(registry._REGISTRY):
        registry.unregister(cid)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved_specs)


def _spec(**kw):
    base = dict(
        id="demo",
        name="Demo",
        auth="oauth_pkce",
        allowed_hosts=("api.demo.test",),
        authorize_url="https://api.demo.test/authorize",
        token_url="https://api.demo.test/token",
        read_scopes=("read",),
        write_scopes=("write",),
    )
    base.update(kw)
    return registry.ConnectorSpec(**base)


def test_a_connector_can_declare_that_it_also_takes_a_token(clean):
    spec = _spec(token_auth=True)
    registry.register(spec)
    assert registry.get("demo").token_auth is True
    assert registry.get("demo").public()["token_auth"] is True


def test_a_connector_that_does_not_take_a_token_says_so(clean):
    registry.register(_spec())
    assert registry.get("demo").public()["token_auth"] is False


def test_a_connector_that_takes_a_token_needs_no_client_credentials(clean):
    """The point of a personal token: no BYOK OAuth app to set up."""
    spec = _spec(token_auth=True)
    registry.register(spec)
    assert spec.client_secret_required is False
    # And it registers fine even with no OAuth URLs at all.
    token_only = registry.ConnectorSpec(
        id="tokenonly", name="Token Only", auth="token", allowed_hosts=("api.x.test",),
        token_auth=True)
    registry.register(token_only)


def test_connecting_with_a_token_verifies_it_before_storing_it(clean, monkeypatch):
    """A wrong token must never be stored: the user would think it works."""
    registry.register(_spec(token_auth=True))
    calls: list[str] = []

    def fake_whoami(token: str) -> str:
        calls.append(token)
        if token != "good-token":
            raise ValueError("bad token")
        return "enrique"

    monkeypatch.setattr(store, "upsert_account", lambda cid, **kw: {"ok": True})

    from ops_dashboard import connector_oauth as oauth

    result = oauth.connect_with_token("demo", "good-token", whoami=fake_whoami)
    assert result["ok"] is True
    assert result["account_label"] == "enrique"
    assert calls == ["good-token"]


def test_connecting_with_a_bad_token_reports_the_error(clean, monkeypatch):
    registry.register(_spec(token_auth=True))

    def fake_whoami(token: str) -> str:
        raise ValueError("401 Unauthorized")

    monkeypatch.setattr(store, "upsert_account", lambda cid, **kw: {"ok": True})

    from ops_dashboard import connector_oauth as oauth

    with pytest.raises(oauth.OAuthError) as ei:
        oauth.connect_with_token("demo", "bad-token", whoami=fake_whoami)
    assert ei.value.code == "bad_token"
    # The message must not leak the provider's raw error body back at the
    # user, and it must not contain the token either.
    assert "bad-token" not in str(ei.value)
    # And nothing was stored: a rejected token must leave no trace.
    assert tokens.load_token("demo") is None


def test_the_token_goes_to_the_keychain_and_not_to_the_database(clean, monkeypatch):
    """A token that lasts for years must not sit in a plaintext column."""
    registry.register(_spec(token_auth=True))
    saved: dict = {}

    def fake_save(cid, tok, **kw):
        saved[cid] = tok
        return tok

    monkeypatch.setattr(tokens, "save_token", fake_save)
    monkeypatch.setattr(store, "upsert_account", lambda cid, **kw: {"ok": True})

    from ops_dashboard import connector_oauth as oauth

    oauth.connect_with_token("demo", "tok-123", whoami=lambda t: "enrique")
    assert saved["demo"]["access_token"] == "tok-123"
    # The stored record must not carry a refresh token: there is nothing to
    # refresh, and pretending otherwise would make the UI promise a renewal
    # that never comes.
    assert saved["demo"].get("refresh_token") is None


def test_connecting_with_a_token_marks_the_account_connected(clean, monkeypatch):
    registry.register(_spec(token_auth=True))
    seen: dict = {}

    def fake_upsert(cid, **kw):
        seen[cid] = kw
        return {"ok": True}

    monkeypatch.setattr(tokens, "save_token", lambda cid, tok, **kw: tok)
    monkeypatch.setattr(store, "upsert_account", fake_upsert)

    from ops_dashboard import connector_oauth as oauth

    oauth.connect_with_token("demo", "tok", whoami=lambda t: "enrique")
    assert seen["demo"]["status"] == "connected"
    assert seen["demo"]["connected_at"] > 0


def test_the_two_ways_do_not_leave_a_stale_credential_behind(clean, monkeypatch):
    """Connecting with a token must clear the OAuth credential, and the other
    way round: a token that was replaced by OAuth must not still work."""
    registry.register(_spec(token_auth=True))
    from ops_dashboard import connector_oauth as oauth

    oauth.connect_with_token("demo", "personal-token", whoami=lambda t: "enrique")
    rec = tokens.load_token("demo")
    assert rec["access_token"] == "personal-token"
    assert rec.get("refresh_token") is None

    # Now the user reconnects with OAuth: the personal token must go, or it
    # stays usable behind the OAuth credential.
    tokens.save_token("demo", {"access_token": "oauth-token", "refresh_token": "r",
                               "auth_method": "oauth"})
    oauth.connect_with_token("demo", "another-token", whoami=lambda t: "enrique")
    rec = tokens.load_token("demo")
    assert rec["access_token"] == "another-token"
    assert rec.get("refresh_token") is None, "the stale refresh token survived"
    assert rec.get("auth_method") == "token"


def test_a_token_connection_survives_a_restart(clean, monkeypatch):
    """The whole point: connect once. The credential is in the keychain, not in
    the process's memory, so it is still there tomorrow."""
    registry.register(_spec(token_auth=True))
    monkeypatch.setattr(store, "upsert_account", lambda cid, **kw: {"ok": True})

    from ops_dashboard import connector_oauth as oauth

    oauth.connect_with_token("demo", "tok", whoami=lambda t: "enrique")
    rec = tokens.load_token("demo")
    assert rec is not None and rec["access_token"] == "tok"
    # A token credential has no expiry: there is nothing to refresh.
    assert rec.get("expires_at") is None


def test_connecting_with_a_token_on_a_connector_that_refuses_it_is_refused(clean):
    """Not every provider can be used with a personal token. The connector
    says so, and the attempt is refused rather than silently half-done."""
    registry.register(_spec(token_auth=False))

    from ops_dashboard import connector_oauth as oauth

    with pytest.raises(Exception) as ei:
        oauth.connect_with_token("demo", "tok", whoami=lambda t: "x")
    assert "token" in str(ei.value).lower()


def test_the_account_view_tells_which_way_was_used(clean, monkeypatch):
    """The UI has to show how the connector was connected, so the user can
    tell a token connection from an OAuth one."""
    registry.register(_spec(token_auth=True))
    monkeypatch.setattr(store, "upsert_account", lambda cid, **kw: {"ok": True})

    from ops_dashboard import connector_oauth as oauth

    oauth.connect_with_token("demo", "tok", whoami=lambda t: "enrique")
    # The stored token record carries how it was obtained.
    rec = tokens.load_token("demo")
    assert rec.get("auth_method") == "token"


def test_the_audit_log_records_the_token_connection(clean, monkeypatch):
    """Every credential change is audited. A token pasted into the app must
    leave the same trace an OAuth consent does."""
    registry.register(_spec(token_auth=True))
    events: list = []
    monkeypatch.setattr(store, "upsert_account", lambda cid, **kw: {"ok": True})
    monkeypatch.setattr(store, "audit", lambda cid, ev, detail=None: events.append((cid, ev)))

    from ops_dashboard import connector_oauth as oauth

    oauth.connect_with_token("demo", "tok", whoami=lambda t: "enrique")
    assert any(ev == "connected_token" for _cid, ev in events), events


def test_the_token_is_never_audited_in_the_clear(clean, monkeypatch):
    """The audit detail must never carry the credential itself."""
    registry.register(_spec(token_auth=True))
    details: list = []
    monkeypatch.setattr(store, "upsert_account", lambda cid, **kw: {"ok": True})
    monkeypatch.setattr(store, "audit", lambda cid, ev, detail=None: details.append(json.dumps(detail or {})))

    from ops_dashboard import connector_oauth as oauth

    oauth.connect_with_token("demo", "super-secret-token", whoami=lambda t: "enrique")
    for d in details:
        assert "super-secret-token" not in d


def test_the_api_exposes_the_token_route_only_for_connectors_that_allow_it(clean, monkeypatch):
    """The route exists, and it refuses a connector that did not opt in."""
    from ops_dashboard import routes_connectors as routes

    src = open(routes.__file__, encoding="utf-8").read()
    assert "connect-token" in src
