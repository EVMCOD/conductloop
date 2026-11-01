"""The user has to know when a credential is about to expire.

"Connect once" is only true for a personal token. An OAuth credential
expires on the provider's schedule: Slack and Microsoft with PKCE expire
after 30 days of inactivity, Google in *Testing* mode after 7, and a Gmail
refresh token stops working when the user changes their password.

The app cannot change that. What it CAN do is tell the truth instead of
failing later: the connector card says the credential expires on a date, and
the app warns before it happens rather than the day the first sync breaks.

So:

- A connector declares how long its credential lasts.
- The account view says when the current credential expires.
- A credential that is about to expire is flagged BEFORE it expires.
- A personal token is never flagged: it lasts until the user revokes it,
  which is the whole point of offering it.
- The warning has to be based on the credential's own record, not on a
  guess: "connected some days ago" is not "expires in 30 days".
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
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "exp.db")
    dbmod.init_db()
    tokens.use_backend(tokens.MemoryBackend())
    saved = dict(registry._REGISTRY)
    for cid in list(registry._REGISTRY):
        registry.unregister(cid)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


def _spec(cid: str = "gh", *, lifetime_days: int = 0):
    return registry.ConnectorSpec(
        id=cid, name="GitHub", auth="token", allowed_hosts=("api.github.test",),
        credential_lifetime_days=lifetime_days)


def test_a_connector_can_declare_how_long_its_credential_lasts():
    spec = _spec(lifetime_days=30)
    assert spec.credential_lifetime_days == 30
    assert spec.public()["credential_lifetime_days"] == 30


def test_a_connector_with_no_lifetime_declares_none():
    """A personal token has no expiry, and saying so is better than guessing."""
    assert _spec().credential_lifetime_days == 0
    assert _spec().public()["credential_lifetime_days"] == 0


def test_the_account_view_says_when_the_credential_expires(env):
    registry.register(_spec(lifetime_days=30))
    store.upsert_account("gh", status="connected", permission="read",
                         granted_scopes=[], account_label=None, client_id=None,
                         last_error=None, connected_at=int(time.time()))
    view = store.get_account("gh")
    assert view["connected_at"] is not None
    expiring = oauth.credential_expiry("gh")
    assert expiring is not None
    assert expiring > time.time()


def test_a_credential_connected_with_a_token_never_expires(env):
    """The whole point of the personal token: it lasts until the user
    revokes it, so the app must not invent an expiry for it."""
    registry.register(_spec(lifetime_days=30))
    tokens.save_token("gh", {"access_token": "tok", "auth_method": "token"})
    store.upsert_account("gh", status="connected", permission="read",
                         granted_scopes=[], account_label=None, client_id=None,
                         last_error=None, connected_at=int(time.time()))
    assert oauth.credential_expiry("gh") is None


def test_a_disconnected_connector_has_no_expiry(env):
    registry.register(_spec(lifetime_days=30))
    assert oauth.credential_expiry("gh") is None


def test_a_credential_about_to_expire_is_flagged(env):
    """The warning has to arrive BEFORE the break, not with it."""
    registry.register(_spec(lifetime_days=30))
    # Connected 29 days ago: one day left.
    store.upsert_account("gh", status="connected", permission="read",
                         granted_scopes=[], account_label=None, client_id=None,
                         last_error=None,
                         connected_at=int(time.time()) - 29 * 86400)
    state = oauth.credential_state("gh")
    assert state["expiring_soon"] is True, state
    assert state["days_left"] is not None and state["days_left"] <= 1, state


def test_a_fresh_credential_is_not_flagged(env):
    registry.register(_spec(lifetime_days=30))
    store.upsert_account("gh", status="connected", permission="read",
                         granted_scopes=[], account_label=None, client_id=None,
                         last_error=None, connected_at=int(time.time()))
    state = oauth.credential_state("gh")
    assert state["expiring_soon"] is False, state
    assert state["days_left"] is not None and state["days_left"] > 1, state


def test_the_warning_uses_the_credential_record_not_the_connection_date(env):
    """An OAuth credential carries its own expiry. That is the truth; the
    connection date is a guess."""
    registry.register(_spec(lifetime_days=30))
    # The credential record says two days; the connection date would say 30
    # (the declared lifetime). The record is the truth.
    expires = int(time.time()) + 2 * 86400
    tokens.save_token("gh", {"access_token": "tok", "auth_method": "oauth",
                             "expires_at": expires})
    store.upsert_account("gh", status="connected", permission="read",
                         granted_scopes=[], account_label=None, client_id=None,
                         last_error=None, connected_at=int(time.time()))
    state = oauth.credential_state("gh")
    assert state["days_left"] is not None and state["days_left"] <= 2, state
    assert state["expiring_soon"] is True, state
    # The declared lifetime did not win over the record.
    assert state["expires_at"] == expires, state


def test_a_credential_without_an_expiry_record_is_not_flagged(env):
    """A credential with no known expiry is not an expiring one."""
    registry.register(_spec(lifetime_days=30))
    tokens.save_token("gh", {"access_token": "tok", "auth_method": "oauth"})
    store.upsert_account("gh", status="connected", permission="read",
                         granted_scopes=[], account_label=None, client_id=None,
                         last_error=None, connected_at=int(time.time()))
    state = oauth.credential_state("gh")
    assert state["expiring_soon"] is False, state


def test_the_state_of_a_disconnected_connector_says_so(env):
    registry.register(_spec(lifetime_days=30))
    state = oauth.credential_state("gh")
    assert state["connected"] is False
    assert state["expiring_soon"] is False


def test_the_state_names_the_auth_method(env):
    """The user has to be able to tell a personal token from an OAuth
    consent: they behave differently."""
    registry.register(_spec(lifetime_days=30))
    tokens.save_token("gh", {"access_token": "tok", "auth_method": "token"})
    store.upsert_account("gh", status="connected", permission="read",
                         granted_scopes=[], account_label=None, client_id=None,
                         last_error=None, connected_at=int(time.time()))
    assert oauth.credential_state("gh")["auth_method"] == "token"


def test_the_view_exposes_the_state_for_the_page(env):
    """The page has to be able to show it."""
    registry.register(_spec(lifetime_days=30))
    store.upsert_account("gh", status="connected", permission="read",
                         granted_scopes=[], account_label=None, client_id=None,
                         last_error=None, connected_at=int(time.time()))
    view = oauth.credential_state("gh")
    assert "days_left" in view
    assert "expiring_soon" in view
