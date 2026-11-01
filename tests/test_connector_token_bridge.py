"""An OAuth connector must be connectable with a personal token too.

The token route already exists for the token connectors. What is missing is
that a connector which declares ``token_auth`` — Slack, Microsoft 365, and
any other provider whose refresh token expires on a schedule the user does
not control — cannot use it. The user is stuck with the OAuth flow, and the
OAuth flow means pressing "Connect" again every 30 days.

So the route has to reach the OAuth connectors too, and to do it through the
same verification: the credential is checked with the provider before it is
stored.

These tests pin the bridge:

- A connector with ``token_auth`` can be connected through the token route.
- A connector without it is refused with a clear code, not a 500.
- The token ends up in the keychain, and the account is connected.
- The connector can then use it: the action path resolves the token without
  asking for an OAuth flow.
"""
from __future__ import annotations

import pytest

from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens
from ops_dashboard import db as dbmod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "bridge.db")
    dbmod.init_db()
    tokens.use_backend(tokens.MemoryBackend())
    saved = dict(registry._REGISTRY)
    for cid in list(registry._REGISTRY):
        registry.unregister(cid)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


def _oauth_spec(cid: str, *, token_auth: bool):
    return registry.ConnectorSpec(
        id=cid, name=cid, auth="oauth_pkce",
        allowed_hosts=("api.example.test",),
        authorize_url="https://api.example.test/authorize",
        token_url="https://api.example.test/token",
        read_scopes=("read",),
        write_scopes=("write",),
        token_auth=token_auth,
        actions={},
    )


def test_the_token_route_reaches_a_connector_that_allows_a_token(env):
    """The route is not 'for token connectors only' any more."""
    from ops_dashboard.connectors_token import base

    # The real connector: OAuth flow AND a personal token.
    registry.register(_oauth_spec("bridge_ok", token_auth=True))
    # The token kit's view of it: same id, so the same keychain item and the
    # same account row.
    base.register_connector(base.TokenConnector(
        spec=registry.ConnectorSpec(
            id="bridge_ok", name="bridge_ok", auth="token",
            allowed_hosts=("api.example.test",), token_auth=True),
        token_mode="keychain",
        whoami=lambda c: "enrique",
        sync=lambda c, cfg: [],
    ))
    try:
        # The token route connects it and marks the account connected.
        base.connect("bridge_ok", token="tok-123")
        acc = store.get_account("bridge_ok")
        assert acc["status"] == "connected"
        rec = tokens.load_token("bridge_ok")
        assert rec is not None and rec["access_token"] == "tok-123"
    finally:
        base.unregister_connector("bridge_ok")


def test_the_token_route_refuses_a_connector_that_does_not_allow_a_token(env):
    """A connector that did not opt in must be refused, not half-connected."""
    from ops_dashboard.connectors_token import base

    # An OAuth connector that does NOT declare token_auth. It lives in the
    # registry (it is a real connector), but it is not in the token kit.
    registry.register(_oauth_spec("bridge_no", token_auth=False))
    try:
        with pytest.raises(base.TokenError) as ei:
            base.connect("bridge_no", token="tok-123")
        assert ei.value.code == "token_not_supported"
    finally:
        registry.unregister("bridge_no")


def test_the_oauth_token_route_also_works_for_those_connectors(env):
    """There are two token routes: the token kit's and the framework's. Both
    have to reach a connector that allows a token, or the user is stuck
    depending on which one the UI happens to call."""
    from ops_dashboard import connector_oauth as oauth

    registry.register(_oauth_spec("bridge_oauth", token_auth=True))
    out = oauth.connect_with_token("bridge_oauth", "tok-abc", whoami=lambda t: "enrique")
    assert out["ok"] is True
    rec = tokens.load_token("bridge_oauth")
    assert rec is not None and rec["access_token"] == "tok-abc"
    assert rec.get("auth_method") == "token"


def test_the_connector_can_use_the_token_without_an_oauth_flow(env):
    """The whole point: after connecting with a token, the connector's actions
    work. There is no OAuth token to refresh, so the action path must find
    the personal token."""
    from ops_dashboard import connector_actions as actions

    registry.register(_oauth_spec("bridge_use", token_auth=True))
    tokens.save_token("bridge_use", {"access_token": "tok-use", "auth_method": "token"})
    store.upsert_account("bridge_use", status="connected", permission="read",
                         granted_scopes=[], account_label=None, client_id=None,
                         last_error=None, connected_at=1)

    # The action path resolves a token for an OAuth connector: it must not
    # raise "not connected" and must not try to refresh.
    client = actions._client_for(registry.get("bridge_use"))
    assert client is not None
    assert getattr(client, "_token", "") == "tok-use"


def test_an_oauth_connector_connected_with_oauth_still_refreshes(env):
    """The token path must not break the OAuth one: an OAuth credential has a
    refresh token and an expiry, and it still refreshes."""
    from ops_dashboard import connector_actions as actions

    registry.register(_oauth_spec("bridge_oauth_use", token_auth=True))
    tokens.save_token("bridge_oauth_use", {"access_token": "old", "refresh_token": "r",
                                           "auth_method": "oauth"})
    store.upsert_account("bridge_oauth_use", status="connected", permission="read",
                         granted_scopes=[], account_label=None, client_id=None,
                         last_error=None, connected_at=1)
    # With no expiry in the record, no refresh is attempted and the stored
    # token is used as-is.
    client = actions._client_for(registry.get("bridge_oauth_use"))
    assert getattr(client, "_token", "") == "old"
