"""Microsoft 365 writes: Mail.Send + Calendars.ReadWrite only with write
permission, queued until confirmed, no invitations, and update/delete only on
events without attendees. No network (ProviderFake, recorded Graph shapes)."""
from __future__ import annotations

import json

import httpx
import pytest
import test_connector_microsoft365 as tm
from connectors_native_fake import connect

from ops_dashboard import connector_actions as actions
from ops_dashboard import connector_oauth as oauth
from ops_dashboard import connector_tokens as tokens

_no_network = tm._no_network
G = tm.G
SEND, RW = "https://graph.microsoft.com/Mail.Send", "https://graph.microsoft.com/Calendars.ReadWrite"
MAIL = {"to": "luis@contoso.example", "cc": ["marta@contoso.example"], "subject": "Presupuesto", "body": "Aprobado.\n"}
EV = {"title": "Revisión", "start": "2026-10-22T10:00:00+02:00", "end": "2026-10-22T11:00:00+02:00", "location": "Sala 1"}


def _created(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert "attendees" not in body
    return httpx.Response(201, json={"id": "AAMkNEW=", "webLink": "https://outlook.office365.com/calendar/item/new", **body})


def _event(attendees):
    def h(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert "attendees" in request.url.params["$select"]
            return httpx.Response(200, json={"id": "AAMkEV1=", "isOrganizer": True, "attendees": attendees})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"id": "AAMkEV1=", **json.loads(request.content)})
    return h


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "m365w.db")
    db.init_db()
    fake = tm.make_fake()
    fake.routes[("POST", f"{G}/me/sendMail")] = lambda r: httpx.Response(202)
    fake.routes[("POST", f"{G}/me/events")] = _created
    for m in ("GET", "PATCH", "DELETE"):
        fake.routes[(m, f"{G}/me/events/AAMkEV1=")] = _event([])
        fake.routes[(m, f"{G}/me/events/AAMkMEET=")] = _event([{"emailAddress": {"address": "luis@contoso.example"}}])
    prev_t, prev_b = oauth.set_transport(fake.transport()), tokens.use_backend(tokens.MemoryBackend())
    try:
        yield fake
    finally:
        tokens.use_backend(prev_b)
        oauth.set_transport(prev_t)


def _writes(fake):
    return [r for r in fake.requests if r.url.host == "graph.microsoft.com" and r.method != "GET"]


def test_read_mode_never_asks_write_scopes_and_blocks(env):
    params = connect("microsoft365", env)
    assert SEND not in params["scope"] and RW not in params["scope"]
    for action, payload in (("send_mail", MAIL), ("create_event", EV)):
        with pytest.raises(actions.ActionError) as e:
            actions.invoke("microsoft365", action, payload)
        assert e.value.code == "write_not_permitted"
    assert _writes(env) == []


def test_write_mode_adds_exactly_two_scopes(env):
    params = connect("microsoft365", env, permission="write")
    assert params["scope"].split() == [*tm.m365.READ_SCOPES, SEND, RW]
    from ops_dashboard import connector_store as store
    assert store.get_account("microsoft365")["permission"] == "write"          # granted == requested


def test_send_mail_pending_then_exact_message_once(env):
    connect("microsoft365", env, permission="write")
    q = actions.invoke("microsoft365", "send_mail", MAIL)
    assert q["status"] == "confirmation_required" and _writes(env) == []
    assert q["summary"].startswith("Outlook · ENVIAR a luis@contoso.example · cc: marta@contoso.example · asunto «Presupuesto»")
    res = actions.confirm(q["action_id"], q["payload_hash"])["result"]
    assert res == {"sent": True, "to": ["luis@contoso.example"], "cc": ["marta@contoso.example"]}
    (w,) = _writes(env)
    body = json.loads(w.content)
    assert body == {"message": {"subject": "Presupuesto", "body": {"contentType": "Text", "content": "Aprobado.\n"},
                                "toRecipients": [{"emailAddress": {"address": "luis@contoso.example"}}],
                                "ccRecipients": [{"emailAddress": {"address": "marta@contoso.example"}}]},
                    "saveToSentItems": True}
    with pytest.raises(actions.ActionError):
        actions.confirm(q["action_id"], q["payload_hash"])
    assert len(_writes(env)) == 1


@pytest.mark.parametrize("bad", [
    {**MAIL, "bcc": "x@evil.example"}, {**MAIL, "to": "Luis <luis@contoso.example>"},
    {**MAIL, "subject": "a\r\nBcc: x@evil.example"}, {**MAIL, "to": [f"u{i}@x.example" for i in range(11)]},
    {**MAIL, "attachments": ["/etc/passwd"]}, {**MAIL, "reply_to_id": "AAMkAD1=="}, {**MAIL, "body": " "},
])
def test_bad_mail_refused_before_queueing(env, bad):
    connect("microsoft365", env, permission="write")
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("microsoft365", "send_mail", bad)
    assert e.value.code == "bad_payload" and actions.list_actions("pending") == [] and _writes(env) == []


def test_create_event_utc_no_invitations(env):
    connect("microsoft365", env, permission="write")
    q = actions.invoke("microsoft365", "create_event", EV)
    assert "2026-10-22T08:00:00Z → 2026-10-22T09:00:00Z" in q["summary"] and "sin invitaciones" in q["summary"]
    actions.confirm(q["action_id"], q["payload_hash"])
    body = json.loads(_writes(env)[0].content)
    assert body == {"subject": "Revisión", "location": {"displayName": "Sala 1"},
                    "start": {"dateTime": "2026-10-22T08:00:00", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-10-22T09:00:00", "timeZone": "UTC"}, "isAllDay": False}


def test_all_day_event(env):
    connect("microsoft365", env, permission="write")
    q = actions.invoke("microsoft365", "create_event", {"title": "Puente", "start": "2026-11-02", "end": "2026-11-03"})
    assert "(día completo)" in q["summary"]
    actions.confirm(q["action_id"], q["payload_hash"])
    body = json.loads(_writes(env)[0].content)
    assert body["isAllDay"] is True and body["start"] == {"dateTime": "2026-11-02T00:00:00", "timeZone": "UTC"}


@pytest.mark.parametrize("bad", [
    {**EV, "attendees": [{"emailAddress": {"address": "x@contoso.example"}}]},
    {**EV, "start": "2026-10-22T10:00:00"}, {**EV, "end": "2026-10-22T09:00:00+02:00"},
    {**EV, "title": "a\nb"}, {**EV, "isOnlineMeeting": True}, {k: v for k, v in EV.items() if k != "title"},
])
def test_bad_events_refused_before_queueing(env, bad):
    connect("microsoft365", env, permission="write")
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("microsoft365", "create_event", bad)
    assert e.value.code == "bad_payload" and _writes(env) == []


def test_update_and_delete_only_without_attendees(env):
    connect("microsoft365", env, permission="write")
    q = actions.invoke("microsoft365", "update_event", {"event_id": "AAMkEV1=", "title": "Comité (movido)"})
    assert "solo si no tiene asistentes" in q["summary"]
    actions.confirm(q["action_id"], q["payload_hash"])
    q = actions.invoke("microsoft365", "delete_event", {"event_id": "AAMkEV1="})
    assert actions.confirm(q["action_id"], q["payload_hash"])["result"]["status"] == "deleted"
    assert [r.method for r in _writes(env)] == ["PATCH", "DELETE"]
    for action, payload in (("update_event", {"event_id": "AAMkMEET=", "title": "x"}),
                            ("delete_event", {"event_id": "AAMkMEET="})):
        q = actions.invoke("microsoft365", action, payload)
        with pytest.raises(actions.ActionError) as e:
            actions.confirm(q["action_id"], q["payload_hash"])
        assert e.value.code == "action_failed"
    assert [r.method for r in _writes(env)] == ["PATCH", "DELETE"]           # the meeting was never touched


def test_graph_error_marks_failed(env):
    connect("microsoft365", env, permission="write")
    env.routes[("POST", f"{G}/me/sendMail")] = lambda r: httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}})
    q = actions.invoke("microsoft365", "send_mail", MAIL)
    with pytest.raises(actions.ActionError):
        actions.confirm(q["action_id"], q["payload_hash"])
    assert actions.list_actions(None)[0]["status"] == "failed"
