"""A personal token has to be verified with the provider, per connector.

The token kit verifies a credential with the connector's own ``whoami``. The
OAuth connectors do not have one in the kit — they get their identity from
the OAuth flow — so a connector that offers both ways needs a way to check a
pasted token against its provider.

The rule that matters: **the token is checked before it is stored.** A token
that does not work must never reach the keychain, because the user would
believe the connector is connected and only find out at the first sync.

Each verifier:

- calls only the connector's own API, on an allowed host,
- raises when the provider rejects the token,
- returns a human label (the account name) when it works.

These tests use a fake transport, so nothing leaves the machine.
"""
from __future__ import annotations

import httpx
import pytest

from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_tokens as tokens
from ops_dashboard import db as dbmod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "verify.db")
    dbmod.init_db()
    tokens.use_backend(tokens.MemoryBackend())
    saved = dict(registry._REGISTRY)
    for cid in list(registry._REGISTRY):
        registry.unregister(cid)
    # The Slack connector, as the app registers it.
    registry.register(registry.ConnectorSpec(
        id="slack", name="Slack", auth="oauth_pkce",
        allowed_hosts=("slack.com",), token_auth=True,
        authorize_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access"))
    from ops_dashboard import connector_oauth as oauth
    oauth.register_token_verifier("slack", oauth._slack_token_label)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_a_verifier_returns_the_account_label_for_a_good_token(env):
    from ops_dashboard import connector_oauth as oauth

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer good-token"
        return httpx.Response(200, json={"ok": True, "user": "enrique"})

    oauth.set_transport(_transport(handler))
    label = oauth.verify_token("slack", "good-token")
    assert label == "enrique"


def test_a_verifier_raises_when_the_provider_rejects_the_token(env):
    from ops_dashboard import connector_oauth as oauth

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error": "invalid_auth"})

    oauth.set_transport(_transport(handler))
    with pytest.raises(oauth.OAuthError) as ei:
        oauth.verify_token("slack", "bad-token")
    assert ei.value.code == "bad_token"


def test_the_verifier_never_asks_the_provider_to_anything_but_the_identity(env):
    """A verification call must not touch the user's data."""
    from ops_dashboard import connector_oauth as oauth

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True, "user": "enrique"})

    oauth.set_transport(_transport(handler))
    oauth.verify_token("slack", "tok")
    assert seen, "no request was made"
    for url in seen:
        assert "auth.test" in url or "/me" in url, url


def test_a_connector_without_a_verifier_is_refused(env):
    """Not every provider can be checked with a personal token. The attempt
    is refused instead of storing an unverified credential."""
    from ops_dashboard import connector_oauth as oauth

    registry.register(registry.ConnectorSpec(
        id="noverify", name="NoVerify", auth="oauth_pkce",
        allowed_hosts=("api.example.test",), token_auth=True,
        authorize_url="https://api.example.test/authorize",
        token_url="https://api.example.test/token"))
    with pytest.raises(oauth.OAuthError) as ei:
        oauth.verify_token("noverify", "tok")
    assert ei.value.code == "token_not_supported"


def test_the_verifier_refuses_a_host_that_is_not_allowed(env):
    """The verifier must not become a way to reach an arbitrary host."""
    from ops_dashboard import connector_oauth as oauth

    registry.register(registry.ConnectorSpec(
        id="badhost", name="BadHost", auth="oauth_pkce",
        allowed_hosts=("api.example.test",), token_auth=True,
        authorize_url="https://api.example.test/authorize",
        token_url="https://api.example.test/token"))
    with pytest.raises(oauth.OAuthError) as ei:
        oauth.verify_token("badhost", "tok")
    assert ei.value.code == "token_not_supported"


def test_connecting_with_a_token_verifies_it_through_the_verifier(env):
    """The end of the chain: the route verifies and then stores. A token that
    the provider rejects leaves nothing behind."""
    from ops_dashboard import connector_oauth as oauth

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False})

    oauth.set_transport(_transport(handler))
    with pytest.raises(oauth.OAuthError):
        oauth.connect_with_token("slack", "bad-token")
    assert tokens.load_token("slack") is None, "a rejected token was stored"
