"""Slack post_message: chat:write only with write permission, queued until the
exact text is confirmed, no mass mentions, no blocks. No network."""
from __future__ import annotations

import json

import httpx
import pytest
import test_connector_slack as ts
from connectors_native_fake import connect

from ops_dashboard import connector_actions as actions
from ops_dashboard import connector_oauth as oauth
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens

_no_network = ts._no_network
MSG = {"channel": "C0GENERAL", "text": "Despliegue terminado ✅ <https://ci.example.com/1|build>"}


def _post(request: httpx.Request) -> httpx.Response:
    assert request.headers["content-type"].startswith("application/json")
    body = json.loads(request.content)
    return httpx.Response(200, json={"ok": True, "channel": body["channel"], "ts": "1792001000.000100",
                                     "message": {"text": body["text"]}})


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "slackw.db")
    db.init_db()
    fake = ts.make_fake()
    fake.routes[("POST", f"{ts.API}/chat.postMessage")] = _post
    prev_t, prev_b = oauth.set_transport(fake.transport()), tokens.use_backend(tokens.MemoryBackend())
    try:
        yield fake
    finally:
        tokens.use_backend(prev_b)
        oauth.set_transport(prev_t)


def _posts(fake):
    return [r for r in fake.requests if r.url.path.endswith("chat.postMessage")]


def test_read_mode_never_asks_chat_write_and_blocks_posting(env):
    params = connect("slack", env)
    assert "chat:write" not in params["scope"]
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("slack", "post_message", MSG)
    assert e.value.code == "write_not_permitted" and _posts(env) == []


def test_write_mode_adds_only_chat_write(env):
    params = connect("slack", env, permission="write")
    assert params["scope"] == "channels:read,channels:history,users:read,chat:write"
    assert store.get_account("slack")["permission"] == "write"


def test_post_pending_until_confirmed_then_sent_once(env):
    connect("slack", env, permission="write")
    q = actions.invoke("slack", "post_message", MSG)
    assert q["status"] == "confirmation_required" and _posts(env) == []
    assert q["summary"].startswith("Slack · PUBLICAR como tú en canal C0GENERAL · ")
    assert "Despliegue terminado ✅" in q["summary"]
    with pytest.raises(actions.ActionError):
        actions.confirm(q["action_id"], "0" * 64)
    assert _posts(env) == []
    res = actions.confirm(q["action_id"], q["payload_hash"])["result"]
    assert res == {"channel": "C0GENERAL", "ts": "1792001000.000100"}
    (req,) = _posts(env)
    body = json.loads(req.content)
    assert body == {"channel": "C0GENERAL", "text": MSG["text"], "unfurl_links": False, "unfurl_media": False,
                    "link_names": False}
    assert req.headers["Authorization"].startswith("Bearer ")
    with pytest.raises(actions.ActionError):
        actions.confirm(q["action_id"], q["payload_hash"])
    assert len(_posts(env)) == 1


def test_thread_reply(env):
    connect("slack", env, permission="write")
    q = actions.invoke("slack", "post_message", {**MSG, "thread_ts": "1792000200.000200"})
    assert "hilo 1792000200.000200 de C0GENERAL" in q["summary"]
    actions.confirm(q["action_id"], q["payload_hash"])
    assert json.loads(_posts(env)[0].content)["thread_ts"] == "1792000200.000200"


@pytest.mark.parametrize("bad", [
    {**MSG, "text": "<!here> ojo"}, {**MSG, "text": "<!channel>"}, {**MSG, "text": "hola @everyone"},
    {**MSG, "text": "equipo <!subteam^S123|@devs>"}, {**MSG, "text": "@channel ya"},
    {**MSG, "text": ""}, {**MSG, "text": "x" * 4001}, {**MSG, "channel": "#general"},
    {**MSG, "channel": "U0LUIS"}, {**MSG, "blocks": [{"type": "section"}]}, {**MSG, "as_user": True},
    {**MSG, "thread_ts": "abc"},
])
def test_bad_posts_refused_before_queueing(env, bad):
    connect("slack", env, permission="write")
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("slack", "post_message", bad)
    assert e.value.code == "bad_payload"
    assert actions.list_actions("pending") == [] and _posts(env) == []


def test_email_addresses_are_not_mass_mentions(env):
    connect("slack", env, permission="write")
    q = actions.invoke("slack", "post_message", {**MSG, "text": "escribe a ana@channel.example"})
    assert q["status"] == "confirmation_required"


def test_slack_ok_false_marks_failed(env):
    connect("slack", env, permission="write")
    env.routes[("POST", f"{ts.API}/chat.postMessage")] = lambda r: httpx.Response(200, json={"ok": False, "error": "not_in_channel"})
    q = actions.invoke("slack", "post_message", MSG)
    with pytest.raises(actions.ActionError) as e:
        actions.confirm(q["action_id"], q["payload_hash"])
    assert e.value.code == "action_failed" and actions.list_actions(None)[0]["status"] == "failed"


def test_downgrade_blocks_queued_post(env):
    connect("slack", env, permission="write")
    q = actions.invoke("slack", "post_message", MSG)
    store.upsert_account("slack", permission="read")
    with pytest.raises(actions.ActionError):
        actions.confirm(q["action_id"], q["payload_hash"])
    assert _posts(env) == []
