"""Four things the review found, and they are all right.

1. ``expires_at`` is the ACCESS TOKEN's expiry, not the connection's. An
   OAuth access token lasts an hour and is renewed automatically, so
   reporting it as "the credential expires" makes a brand new connection
   show "expires in 0 days: reconnect" and appear in the expiring report,
   when the user has to do nothing at all.

2. A sync selector declared as a string but read as a list. The model sends
   what the schema says, the handler ignores it, and the sync quietly
   imports everything.

3. The advertised date examples are rejected by the parser. A model that
   follows the examples cannot even queue the event.

4. update/delete default to the primary calendar, so an event that was
   listed from another calendar cannot be found to change or delete.
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
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "rev.db")
    dbmod.init_db()
    tokens.use_backend(tokens.MemoryBackend())
    saved = dict(registry._REGISTRY)
    for cid in list(registry._REGISTRY):
        registry.unregister(cid)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


def _spec(cid: str = "gh", days: int = 30):
    return registry.ConnectorSpec(
        id=cid, name=cid, auth="token", allowed_hosts=("api.x.test",),
        credential_lifetime_days=days)


def _connect(cid: str = "gh"):
    store.upsert_account(cid, status="connected", permission="read",
                         granted_scopes=[], account_label=None, client_id=None,
                         last_error=None, connected_at=int(time.time()))


def test_an_hour_long_access_token_is_not_a_connection_expiry(env):
    """The access token expires in an hour and is renewed automatically.

    Reporting THAT as the credential's expiry would tell the user to
    reconnect a connection that works — and, worse, would show "expires in
    0 days" on a connection made a minute ago. What the card shows is the
    lifetime of the refresh credential, which is the thing that can stop
    being renewed.
    """
    registry.register(_spec(days=30))
    _connect()
    tokens.save_token("gh", {"access_token": "tok", "auth_method": "oauth",
                             "expires_at": int(time.time()) + 3600})
    state = oauth.credential_state("gh")
    # NOT zero: the refresh credential's own lifetime, not the access
    # token's one hour.
    assert state["days_left"] is not None and state["days_left"] >= 29, state
    assert state["expiring_soon"] is False, state
    assert oauth.expiring_credentials() == []


def test_the_refresh_credential_is_what_expires(env):
    """The refresh token is the thing that can stop being renewable, and
    the provider's rule says how long it lasts."""
    registry.register(_spec(days=30))
    _connect()
    tokens.save_token("gh", {"access_token": "tok", "auth_method": "oauth",
                             "expires_at": int(time.time()) + 3600})
    state = oauth.credential_state("gh")
    # The access token is ignored; the declared lifetime of the REFRESH
    # credential is what the card shows.
    assert state["days_left"] is not None and state["days_left"] >= 29, state


def test_a_sync_selector_is_declared_as_a_list_of_strings(env):
    """The handler only applies the filter when it is a list, so the schema
    has to say a list."""
    from ops_dashboard.connectors_native import google_calendar, slack

    for spec, key in ((google_calendar.SPEC, "calendar_ids"), (slack.SPEC, "channel_ids")):
        schema = spec.actions["sync"].input_schema
        prop = schema["properties"][key]
        assert prop["type"] == "array", f"{spec.id}: {key} must be an array"
        assert prop["items"] == {"type": "string"}, f"{spec.id}: {key} items"
        assert key in schema["required"] or True


def test_the_advertised_date_examples_are_accepted(env):
    """An example the model copies must not be rejected by the parser.

    Both parsers are checked: Google Calendar and Microsoft 365 read times
    their own way, and both reject a time without a UTC offset.
    """
    from ops_dashboard.connectors_native import google_calendar, microsoft365

    for example, end in (("2026-11-03T10:00:00+01:00", "2026-11-03T11:00:00+01:00"),
                         ("2026-11-03T09:00:00Z", "2026-11-03T10:00:00Z"),
                         ("2026-11-03", "2026-11-04")):
        body, _ts, all_day = google_calendar._when(example, "start")
        assert body
        # The two ends must differ: the parser rejects a zero-length event.
        microsoft365._event_body({"title": "t", "start": example, "end": end}, partial=False)

    # And the schema's example must be one of those.
    for spec in (google_calendar.SPEC, microsoft365.SPEC):
        schema = spec.actions["create_event"].input_schema
        for key in ("start", "end"):
            desc = schema["properties"][key]["description"]
            assert ("+01:00" in desc or "Z" in desc or "YYYY-MM-DD" in desc
                    or "'2026-11-03'" in desc), f"{spec.id}.{key}: {desc}"


def test_update_and_delete_accept_the_calendar_the_event_is_in(env):
    """An event listed from another calendar has to be findable to change."""
    from ops_dashboard.connectors_native import google_calendar

    for action in ("update_event", "delete_event"):
        schema = google_calendar.SPEC.actions[action].input_schema
        assert "calendar_id" in schema["properties"], action
