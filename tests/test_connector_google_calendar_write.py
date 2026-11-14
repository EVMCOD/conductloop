"""Google Calendar writes: queued, confirmed by the exact payload hash, no
invitations, write scope only when asked. No network (ProviderFake)."""
from __future__ import annotations

import json

import httpx
import pytest
import test_connector_google_calendar as tg
from connectors_native_fake import ProviderFake, connect

from ops_dashboard import connector_actions as actions
from ops_dashboard import connector_oauth as oauth
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens

_no_network = tg._no_network
EV = f"{tg.API}/calendars/primary/events"
OWNED = "https://www.googleapis.com/auth/calendar.events.owned"
NEW = {"title": "Revisión #55", "start": "2026-10-22T10:00:00+02:00", "end": "2026-10-22T11:00:00+02:00",
       "location": "Sala 2", "description": "Agenda:\n- conectores"}


def _write(request: httpx.Request) -> httpx.Response:
    assert request.url.params["sendUpdates"] == "none"
    if request.method == "DELETE":
        return httpx.Response(204)
    body = json.loads(request.content)
    assert "attendees" not in body
    return httpx.Response(200, json={"id": "ev123", "status": "confirmed", "htmlLink": "https://www.google.com/calendar/event?eid=x",
                                     **body})


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gcalw.db")
    db.init_db()
    fake = ProviderFake(token_url="https://oauth2.googleapis.com/token",
                        hosts=("oauth2.googleapis.com", "accounts.google.com", "www.googleapis.com"),
                        client_secret=tg.SECRET,
                        routes={("POST", EV): _write, ("PATCH", f"{EV}/ev123"): _write, ("DELETE", f"{EV}/ev123"): _write})
    prev_t, prev_b = oauth.set_transport(fake.transport()), tokens.use_backend(tokens.MemoryBackend())
    try:
        yield fake
    finally:
        tokens.use_backend(prev_b)
        oauth.set_transport(prev_t)


def _api_writes(fake):
    return [r for r in fake.requests if r.method != "GET" and r.url.host != "oauth2.googleapis.com"]


def test_read_connection_never_asks_write_scope_and_blocks_writes(env):
    params = connect("google_calendar", env, client_secret=tg.SECRET)
    assert OWNED not in params["scope"]
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("google_calendar", "create_event", NEW)
    assert e.value.code == "write_not_permitted" and _api_writes(env) == []


def test_write_permission_adds_only_events_owned(env):
    params = connect("google_calendar", env, client_secret=tg.SECRET, permission="write")
    assert params["scope"].split() == [*tg.gcal.READ_SCOPES, OWNED]
    assert "auth/calendar " not in params["scope"] + " " and not params["scope"].endswith("auth/calendar")


def test_create_is_pending_until_confirmed_with_the_same_hash(env):
    connect("google_calendar", env, client_secret=tg.SECRET, permission="write")
    q = actions.invoke("google_calendar", "create_event", NEW)
    assert q["status"] == "confirmation_required" and _api_writes(env) == []
    assert "Revisión #55" in q["summary"] and "sin invitaciones" in q["summary"]
    assert "2026-10-22T10:00:00+02:00 → 2026-10-22T11:00:00+02:00" in q["summary"]
    with pytest.raises(actions.ActionError) as e:
        actions.confirm(q["action_id"], "0" * 64)
    assert e.value.code == "hash_mismatch" and _api_writes(env) == []
    done = actions.confirm(q["action_id"], q["payload_hash"])
    assert done["status"] == "done" and done["result"]["id"] == "ev123"
    (w,) = _api_writes(env)
    body = json.loads(w.content)
    assert w.method == "POST" and body == {"summary": "Revisión #55", "location": "Sala 2",
                                           "description": "Agenda:\n- conectores",
                                           "start": {"dateTime": "2026-10-22T10:00:00+02:00"},
                                           "end": {"dateTime": "2026-10-22T11:00:00+02:00"}}
    with pytest.raises(actions.ActionError):
        actions.confirm(q["action_id"], q["payload_hash"])          # single use
    assert len(_api_writes(env)) == 1


def test_expired_confirmation_never_writes(env):
    connect("google_calendar", env, client_secret=tg.SECRET, permission="write")
    q = actions.invoke("google_calendar", "create_event", NEW, now=1000.0)
    with pytest.raises(actions.ActionError) as e:
        actions.confirm(q["action_id"], q["payload_hash"], now=1000.0 + 15 * 60 + 1)
    assert e.value.code == "expired" and _api_writes(env) == []


@pytest.mark.parametrize("bad", [
    {**NEW, "attendees": [{"email": "luis@example.com"}]},
    {**NEW, "title": "x\r\nBcc: a@b.c"},
    {**NEW, "start": "2026-10-22T10:00:00"},                    # no offset
    {**NEW, "end": "2026-10-22T09:00:00+02:00"},                # before start
    {**NEW, "end": "2026-11-22T09:00:00+02:00"},                # > 14 days
    {**NEW, "end": "2026-10-23"},                               # mixed kinds
    {**NEW, "calendar_id": "../users/me"},
    {k: v for k, v in NEW.items() if k != "title"},
])
def test_bad_payloads_are_refused_before_queueing(env, bad):
    connect("google_calendar", env, client_secret=tg.SECRET, permission="write")
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("google_calendar", "create_event", bad)
    assert e.value.code == "bad_payload"
    assert actions.list_actions("pending") == [] and _api_writes(env) == []


def test_all_day_update_and_delete(env):
    connect("google_calendar", env, client_secret=tg.SECRET, permission="write")
    q = actions.invoke("google_calendar", "update_event",
                       {"event_id": "ev123", "title": "Off-site", "start": "2026-10-23", "end": "2026-10-24"})
    assert "cambiar evento ev123" in q["summary"] and "sin avisar" in q["summary"]
    actions.confirm(q["action_id"], q["payload_hash"])
    w = _api_writes(env)[-1]
    assert w.method == "PATCH" and json.loads(w.content) == {"summary": "Off-site", "start": {"date": "2026-10-23"},
                                                             "end": {"date": "2026-10-24"}}
    with pytest.raises(actions.ActionError):
        actions.invoke("google_calendar", "update_event", {"event_id": "ev123"})       # nothing to change
    q = actions.invoke("google_calendar", "delete_event", {"event_id": "ev123"})
    assert q["summary"].startswith("Google Calendar · BORRAR evento ev123")
    assert actions.confirm(q["action_id"], q["payload_hash"])["result"]["status"] == "deleted"
    assert [r.method for r in _api_writes(env)] == ["PATCH", "DELETE"]
    events = [a["event"] for a in store.list_audit(connector_id="google_calendar")]
    assert events.count("write_confirmed") == 2 and events.count("write_done") == 2


def test_provider_error_marks_action_failed(env):
    connect("google_calendar", env, client_secret=tg.SECRET, permission="write")
    env.routes[("POST", EV)] = lambda r: httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})
    q = actions.invoke("google_calendar", "create_event", NEW)
    with pytest.raises(actions.ActionError) as e:
        actions.confirm(q["action_id"], q["payload_hash"])
    assert e.value.code == "action_failed"
    assert actions.list_actions(None)[0]["status"] == "failed"


def test_prune_does_not_treat_underscore_as_wildcard(env):
    from ops_dashboard.connectors_native import _ingest
    _ingest.to_vault(source="google_calendar", path="google_calendar/a_b/1", title="t", content="x", mtime=1)
    _ingest.to_vault(source="google_calendar", path="google_calendar/axb/1", title="t", content="y", mtime=1)
    assert _ingest.prune("google_calendar", "google_calendar/a_b/", set()) == 1
    _ingest.upsert_calendar_event(uid="gcal:a_b:1", title="t", starts_at=10, all_day=False, source="google_calendar")
    _ingest.upsert_calendar_event(uid="gcal:axb:1", title="t", starts_at=10, all_day=False, source="google_calendar")
    assert _ingest.prune_calendar("google_calendar", "gcal:a_b:", 0, 100, set()) == 1
    from ops_dashboard import db
    with db.db() as c:
        assert [r[0] for r in c.execute("SELECT uid FROM calendar_events")] == ["gcal:axb:1"]
        assert [r[0] for r in c.execute("SELECT path FROM documents")] == ["google_calendar/axb/1"]
