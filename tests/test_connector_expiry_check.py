"""The app has to warn BEFORE the credential breaks.

A connector whose refresh token is about to expire still works today and
fails tomorrow. The user cannot act on a failure they did not see coming, so
the app checks the credentials it knows about and says which ones are about
to go.

The check runs on demand (the page asks) and it is cheap: it reads the
stored record, it does not call the provider. Calling the provider on every
page load would be a network call per connector per page, and it would turn
a read into a side effect.

The rules:

- Only connected connectors are checked.
- A personal token is never reported as expiring: it lasts until revoked.
- A credential with no known expiry is not reported either.
- The result names the connector and says how many days are left.
- It never returns the credential itself.
"""
from __future__ import annotations

import time

import pytest

from ops_dashboard import connector_oauth as oauth
from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens
from ops_dashboard import db as dbmod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "hc.db")
    dbmod.init_db()
    tokens.use_backend(tokens.MemoryBackend())
    saved = dict(registry._REGISTRY)
    for cid in list(registry._REGISTRY):
        registry.unregister(cid)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


def _spec(cid: str, days: int = 30):
    return registry.ConnectorSpec(
        id=cid, name=cid.title(), auth="token", allowed_hosts=("api.x.test",),
        credential_lifetime_days=days)


def _connect(cid: str, days_ago: int = 0):
    store.upsert_account(cid, status="connected", permission="read",
                         granted_scopes=[], account_label=None, client_id=None,
                         last_error=None, connected_at=int(time.time()) - days_ago * 86400)


def test_a_credential_about_to_expire_is_reported(env):
    registry.register(_spec("gh"))
    _connect("gh", days_ago=29)
    report = oauth.expiring_credentials()
    assert len(report) == 1, report
    assert report[0]["connector_id"] == "gh"
    assert report[0]["days_left"] is not None


def test_a_fresh_credential_is_not_reported(env):
    registry.register(_spec("gh"))
    _connect("gh", days_ago=1)
    assert oauth.expiring_credentials() == []


def test_a_personal_token_is_never_reported(env):
    """It lasts until the user revokes it: there is nothing to warn about."""
    registry.register(_spec("gh"))
    tokens.save_token("gh", {"access_token": "tok", "auth_method": "token"})
    _connect("gh", days_ago=100)
    assert oauth.expiring_credentials() == []


def test_a_disconnected_connector_is_not_reported(env):
    registry.register(_spec("gh"))
    assert oauth.expiring_credentials() == []


def test_the_report_never_carries_the_credential(env):
    import json

    registry.register(_spec("gh"))
    tokens.save_token("gh", {"access_token": "ghp_secret", "auth_method": "oauth",
                             "expires_at": int(time.time()) + 86400})
    _connect("gh", days_ago=29)
    report = oauth.expiring_credentials()
    assert "ghp_secret" not in json.dumps(report)


def test_the_report_says_what_to_do(env):
    """The warning has to tell the user what to do about it."""
    registry.register(_spec("gh"))
    _connect("gh", days_ago=29)
    row = oauth.expiring_credentials()[0]
    assert row.get("action"), row
    assert "connect" in str(row["action"]).lower()


def test_the_check_makes_no_network_call(env, monkeypatch):
    """A page load must not turn into a request per connector."""
    called = []

    def boom(*a, **k):
        called.append(1)
        raise AssertionError("the expiry check called the provider")

    monkeypatch.setattr(oauth, "refresh", boom)
    registry.register(_spec("gh"))
    _connect("gh", days_ago=29)
    oauth.expiring_credentials()
    assert not called
