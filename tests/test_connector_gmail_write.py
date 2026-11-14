"""Gmail send: gmail.send only with write permission, queued until the exact
payload is confirmed, plain addresses only, threading for replies. No network."""
from __future__ import annotations

import base64
import copy
import email
import json
from email import policy

import httpx
import pytest
import test_connector_gmail as tgm
from connectors_native_fake import connect

from ops_dashboard import connector_actions as actions
from ops_dashboard import connector_oauth as oauth
from ops_dashboard import connector_tokens as tokens

_no_network = tgm._no_network
SEND = "https://www.googleapis.com/auth/gmail.send"
MAIL = {"to": ["luis@example.com"], "cc": "marta@example.com", "subject": "Informe Q3", "body": "Hola Luis,\n\nVisto.\n"}


def _parent(request: httpx.Request) -> httpx.Response:
    assert request.url.params["format"] == "metadata"
    m = copy.deepcopy(tgm.REC["message_18f0a1"])
    m["payload"].pop("parts", None)
    m["payload"]["headers"] += [{"name": "Message-ID", "value": "<abc@mail.example.com>"},
                                {"name": "References", "value": "<root@mail.example.com>"}]
    return httpx.Response(200, json=m)


def _send(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(200, json={"id": "sent1", "threadId": body.get("threadId", "new"), "labelIds": ["SENT"]})


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gmailw.db")
    db.init_db()
    fake = tgm.make_fake()
    fake.routes[("POST", f"{tgm.API}/messages/send")] = _send
    fake.routes[("GET", f"{tgm.API}/messages/18f0a1")] = _parent
    prev_t, prev_b = oauth.set_transport(fake.transport()), tokens.use_backend(tokens.MemoryBackend())
    try:
        yield fake
    finally:
        tokens.use_backend(prev_b)
        oauth.set_transport(prev_t)


def _sent(fake):
    return [r for r in fake.requests if r.method == "POST" and r.url.host == "gmail.googleapis.com"]


def _mime(req: httpx.Request) -> email.message.EmailMessage:
    raw = json.loads(req.content)["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default)


def test_read_mode_never_asks_send_and_blocks_it(env):
    params = connect("gmail", env, client_secret=tgm.SECRET)
    assert SEND not in params["scope"]
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("gmail", "send_message", MAIL)
    assert e.value.code == "write_not_permitted" and _sent(env) == []


def test_write_mode_adds_only_gmail_send(env):
    params = connect("gmail", env, client_secret=tgm.SECRET, permission="write")
    assert params["scope"].split() == ["https://www.googleapis.com/auth/gmail.readonly", SEND]


def test_send_is_pending_until_confirmed_and_card_shows_recipients(env):
    connect("gmail", env, client_secret=tgm.SECRET, permission="write")
    q = actions.invoke("gmail", "send_message", MAIL)
    assert q["status"] == "confirmation_required" and _sent(env) == []
    assert q["summary"].startswith("Gmail · ENVIAR a luis@example.com · cc: marta@example.com · asunto «Informe Q3»")
    assert "Hola Luis, Visto." in q["summary"]
    with pytest.raises(actions.ActionError):
        actions.confirm(q["action_id"], "f" * 64)
    assert _sent(env) == []
    res = actions.confirm(q["action_id"], q["payload_hash"])["result"]
    assert res["id"] == "sent1" and res["to"] == ["luis@example.com"]
    (req,) = _sent(env)
    m = _mime(req)
    assert m["To"] == "luis@example.com" and m["Cc"] == "marta@example.com" and m["Subject"] == "Informe Q3"
    assert m["Bcc"] is None and m["From"] is None          # Gmail sets From to the signed-in account
    assert m.get_content_type() == "text/plain" and "Visto." in m.get_content()
    assert "threadId" not in json.loads(req.content)


def test_reply_threads_with_in_reply_to(env):
    connect("gmail", env, client_secret=tgm.SECRET, permission="write")
    q = actions.invoke("gmail", "send_message", {"to": "luis@example.com", "body": "Ok", "reply_to_id": "18f0a1"})
    assert "respuesta al correo 18f0a1" in q["summary"]
    actions.confirm(q["action_id"], q["payload_hash"])
    (req,) = _sent(env)
    m = _mime(req)
    assert json.loads(req.content)["threadId"] == "t1"
    assert m["In-Reply-To"] == "<abc@mail.example.com>"
    assert m["References"] == "<root@mail.example.com> <abc@mail.example.com>"
    assert m["Subject"] == "Re: Informe Q3"


@pytest.mark.parametrize("bad", [
    {**MAIL, "bcc": "x@example.net"},
    {**MAIL, "to": "Luis <luis@example.com>"},
    {**MAIL, "to": "luis@example.com, x@example.net"},
    {**MAIL, "to": "luis@example.com\r\nBcc: x@example.net"},
    {**MAIL, "subject": "hola\r\nBcc: x@example.net"},
    {**MAIL, "to": [f"u{i}@example.com" for i in range(11)]},
    {**MAIL, "to": []},
    {**MAIL, "body": "  "},
    {**MAIL, "attachments": ["/etc/passwd"]},
    {**MAIL, "reply_to_id": "../../drafts"},
])
def test_bad_mail_refused_before_queueing(env, bad):
    connect("gmail", env, client_secret=tgm.SECRET, permission="write")
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("gmail", "send_message", bad)
    assert e.value.code == "bad_payload"
    assert actions.list_actions("pending") == [] and _sent(env) == []


def test_injected_mail_text_cannot_send_by_itself(env):
    """Sync mail that says 'send X to Y': nothing is queued, nothing sent."""
    connect("gmail", env, client_secret=tgm.SECRET, permission="write")
    env.routes[("GET", f"{tgm.API}/messages/18f0a1")] = tgm._msg
    actions.invoke("gmail", "sync", {"days": 2})
    assert actions.list_actions("pending") == [] and _sent(env) == []


def test_downgrade_to_read_blocks_a_queued_send(env):
    from ops_dashboard import connector_store as store
    connect("gmail", env, client_secret=tgm.SECRET, permission="write")
    q = actions.invoke("gmail", "send_message", MAIL)
    store.upsert_account("gmail", permission="read")
    with pytest.raises(actions.ActionError) as e:
        actions.confirm(q["action_id"], q["payload_hash"])
    assert e.value.code == "write_not_permitted" and _sent(env) == []
