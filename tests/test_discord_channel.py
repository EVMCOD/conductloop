"""Discord channel (step 1): mocked REST only — no live Discord, no Gateway."""
from __future__ import annotations

import json
import uuid

import pytest

from ops_dashboard import credentials, db
from ops_dashboard.channels import discord, store

# Three base64url segments — passes credentials shape check; never a real token.
FAKE_TOKEN = ("x" * 24) + "." + ("y" * 6) + "." + ("z" * 27)  # built at runtime; never a real token


class FakeDiscord:
    def __init__(self):
        self.calls: list[tuple] = []
        self.sent: list[tuple[str, str]] = []
        self.dms_opened: list[str] = []
        self.me = {"id": "99", "username": "clbot"}
        self.error: dict | None = None  # {"__http_status", "message", "code"} for next call
        self.fail_messages: dict[str, dict] = {}  # channel_id -> error dict
        self.dm_map: dict[str, str] = {}
        self.next_dm = 5000

    def __call__(self, method, url, payload, timeout, headers):
        self.calls.append((method, url, payload))
        assert headers.get("Authorization", "").startswith("Bot ")
        blob = json.dumps({"u": url, "p": payload or {}})
        assert FAKE_TOKEN not in blob
        if self.error:
            err, self.error = self.error, None
            return err
        if method == "GET" and url.endswith("/users/@me"):
            return dict(self.me)
        if method == "POST" and url.endswith("/users/@me/channels"):
            uid = str((payload or {}).get("recipient_id"))
            self.dms_opened.append(uid)
            cid = self.dm_map.setdefault(uid, str(self.next_dm))
            if cid == str(self.next_dm):
                self.next_dm += 1
            return {"id": cid, "type": 1}
        if method == "POST" and "/channels/" in url and url.endswith("/messages"):
            cid = url.rstrip("/").split("/")[-2]
            if cid in self.fail_messages:
                return self.fail_messages[cid]
            content = (payload or {}).get("content") or ""
            self.sent.append((cid, content))
            return {"id": "1", "content": content}
        return {}


def _msg(*, channel_id="10", content="/help", author_id="1", username="alice",
         guild_id=None, bot=False, mentions=None, channel_name=None):
    return {
        "id": "m1",
        "channel_id": str(channel_id),
        "guild_id": str(guild_id) if guild_id else None,
        "channel_name": channel_name,
        "content": content,
        "author": {"id": str(author_id), "username": username, "bot": bot},
        "mentions": mentions or [],
    }


@pytest.fixture()
def env(monkeypatch):
    from ops_dashboard import secrets_store

    db.init_db()
    with db.db() as conn:
        conn.execute("DELETE FROM channel_settings")
        conn.execute("DELETE FROM channel_peers")
        conn.execute("DELETE FROM channel_members")
        conn.execute("DELETE FROM channel_deliveries")
    items: dict[str, str] = {}
    monkeypatch.setattr(secrets_store, "keychain_available", lambda: True)
    monkeypatch.setattr(secrets_store, "keychain_get", lambda name: items.get(name))
    monkeypatch.setattr(secrets_store, "keychain_set",
                        lambda name, value: items.__setitem__(name, value) or True)
    monkeypatch.setattr(secrets_store, "keychain_delete",
                        lambda name: items.pop(name, None) is not None)
    fake = FakeDiscord()
    monkeypatch.setattr(discord, "_urllib_transport", fake)
    team = db.create_team(f"dc-{uuid.uuid4().hex[:12]}", "Discord test team")
    agent_id = db.add_member(team, "Scout", type="ai_agent", role="research")
    wakes: list[tuple[int, str]] = []

    def wake(aid: int, prompt: str) -> dict:
        wakes.append((aid, prompt))
        return {"ok": True, "response": f"answer to: {prompt}"}

    ceo_asks: list[str] = []

    def holding_ceo(prompt: str) -> str:
        ceo_asks.append(prompt)
        return f"CEO: {prompt}"

    poller = discord.Poller(transport=fake, wake=wake, holding_ceo=holding_ceo)
    poller._submit = lambda fn: fn()
    yield {"fake": fake, "items": items, "agent_id": agent_id, "wakes": wakes,
           "poller": poller, "ceo_asks": ceo_asks, "wake": wake, "holding_ceo": holding_ceo}
    with db.db() as conn:
        conn.execute("DELETE FROM channel_settings")
        conn.execute("DELETE FROM channel_peers")
        conn.execute("DELETE FROM channel_members")
        conn.execute("DELETE FROM channel_deliveries")


def _turn_on(env) -> None:
    credentials.set_secret(discord.TOKEN_NAME, FAKE_TOKEN)
    assert discord.enable()["ok"] is True


def _handler(env):
    h = discord.Handler(discord.BotApi(FAKE_TOKEN, env["fake"]),
                        wake=env["wake"], holding_ceo=env["holding_ceo"])
    h.submit = lambda fn: fn()
    return h


def _run(env, status="done", text="ok") -> int:
    tid = db.create_task(None, f"task {uuid.uuid4().hex[:6]}", "do it",
                         agent_member_id=env["agent_id"])
    rid = db.start_run(tid, "mock", "m")
    assert rid
    assert db.finish_run(rid, status=status, output_text=text,
                         error="boom" if status == "error" else None)
    return rid


def test_off_by_default_makes_no_network_call(env):
    assert discord.status()["enabled"] is False
    assert env["poller"].run_once() == discord.IDLE_WAIT
    assert env["fake"].calls == []


def test_enable_checks_the_token_with_discord_first(env):
    credentials.set_secret(discord.TOKEN_NAME, FAKE_TOKEN)
    out = discord.enable()
    assert out["ok"] is True
    assert out["bot_username"] == "clbot"
    assert any(c[0] == "GET" and c[1].endswith("/users/@me") for c in env["fake"].calls)


def test_enable_rejects_a_bad_token(env):
    credentials.set_secret(discord.TOKEN_NAME, FAKE_TOKEN)
    env["fake"].error = {"__http_status": 401, "message": "401: Unauthorized", "code": 0}
    out = discord.enable()
    assert out["ok"] is False and out["error"] == "token_rejected"
    assert discord.status()["enabled"] is False


def test_a_stranger_gets_one_fixed_sentence_and_no_agent(env):
    _turn_on(env)
    h = _handler(env)
    assert h.handle(_msg(content="hi")) == "stranger"
    assert env["fake"].sent and "ConductLoop" in env["fake"].sent[0][1]
    assert env["ceo_asks"] == [] and env["wakes"] == []
    assert h.handle(_msg(content="hi again")) == "stranger:quiet"


def test_pairing_code_is_one_use(env):
    _turn_on(env)
    code = store.new_pairing_code(discord.CHANNEL)["code"]
    h = _handler(env)
    assert h.handle(_msg(content=f"/pair {code}")) == "paired"
    assert store.peer(discord.CHANNEL, "10") is not None
    assert h.handle(_msg(channel_id="11", content=f"/pair {code}")) == "pair-refused"


def test_paired_dm_asks_holding_ceo_by_default(env):
    _turn_on(env)
    code = store.new_pairing_code(discord.CHANNEL)["code"]
    h = _handler(env)
    assert h.handle(_msg(content=f"/pair {code}")) == "paired"
    assert h.handle(_msg(content="hello there")) == "asked:holding-ceo"
    assert env["ceo_asks"] == ["hello there"]
    assert any("CEO: hello there" in c[1] for c in env["fake"].sent)


def test_use_overrides_and_use_ceo_goes_back(env):
    _turn_on(env)
    code = store.new_pairing_code(discord.CHANNEL)["code"]
    h = _handler(env)
    h.handle(_msg(content=f"/pair {code}"))
    aid = env["agent_id"]
    assert h.handle(_msg(content=f"/use {aid}")) == "use"
    assert h.handle(_msg(content="task")) == f"asked:{aid}"
    assert env["wakes"] == [(aid, "task")]
    assert h.handle(_msg(content="/use ceo")) == "use:default"
    assert h.handle(_msg(content="again")) == "asked:holding-ceo"


def test_guild_only_mentions_and_commands(env):
    _turn_on(env)
    code = store.new_pairing_code(discord.CHANNEL)["code"]
    h = _handler(env)
    assert h.handle(_msg(content=f"/pair {code}", guild_id="g1",
                         channel_name="general", author_id="1")) == "paired"
    assert h.handle(_msg(content="hello", guild_id="g1")) == "ignored:not-addressed"
    assert h.handle(_msg(content="hi <@99>", guild_id="g1",
                         mentions=[{"id": "99", "username": "clbot"}])) == "asked:holding-ceo"
    assert h.handle(_msg(content="/help", guild_id="g1")) == "help"


def test_people_not_on_the_list_get_a_fixed_notice_once(env):
    _turn_on(env)
    code = store.new_pairing_code(discord.CHANNEL)["code"]
    h = _handler(env)
    h.handle(_msg(content=f"/pair {code}", guild_id="g1", author_id="1", username="owner"))
    assert h.handle(_msg(content="/help", guild_id="g1", author_id="2",
                         username="bob")) == "denied:not-on-list"
    n = len(env["fake"].sent)
    assert h.handle(_msg(content="/help", guild_id="g1", author_id="2")) == "denied:quiet"
    assert len(env["fake"].sent) == n
    assert env["ceo_asks"] == [] and env["wakes"] == []


def test_snowflake_user_ids_are_accepted(env):
    """Discord snowflakes are 17–19 digits; store used to cap at 15 (Telegram)."""
    _turn_on(env)
    code = store.new_pairing_code(discord.CHANNEL)["code"]
    h = _handler(env)
    snow = "123456789012345678"
    assert h.handle(_msg(content=f"/pair {code}", guild_id="g1",
                         author_id=snow, channel_id="20")) == "paired"
    assert store.member_allowed(discord.CHANNEL, "20", snow)


def test_group_results_go_only_to_allowed_members_by_dm(env):
    _turn_on(env)
    code = store.new_pairing_code(discord.CHANNEL)["code"]
    h = _handler(env)
    h.handle(_msg(content=f"/pair {code}", guild_id="g1", author_id="1", channel_id="20"))
    store.allow_member(discord.CHANNEL, "20", "7", "bob", by="app")
    import time
    store.update(discord.CHANNEL, results_since=int(time.time()) - 10)
    _run(env, "done", "the result")
    sent = discord.push_results(discord.BotApi(FAKE_TOKEN, env["fake"]),
                                store.settings(discord.CHANNEL))
    assert sent >= 1
    assert "7" in env["fake"].dms_opened
    assert any("the result" in c[1] for c in env["fake"].sent)
    # guild channel itself never gets the result
    assert not any(c[0] == "20" and "the result" in c[1] for c in env["fake"].sent)


def test_unreachable_members_are_flagged(env):
    _turn_on(env)
    code = store.new_pairing_code(discord.CHANNEL)["code"]
    _handler(env).handle(_msg(content=f"/pair {code}", guild_id="g1",
                              author_id="1", channel_id="20"))
    store.allow_member(discord.CHANNEL, "20", "7", "bob", by="app")
    import time
    store.update(discord.CHANNEL, results_since=int(time.time()) - 10)
    _run(env, "done", "r")
    # open DM succeeds; sending to that DM channel fails with 50007
    env["fake"].dm_map["7"] = "7777"
    env["fake"].fail_messages["7777"] = {
        "__http_status": 403, "message": "Cannot send messages to this user", "code": 50007}
    discord.push_results(discord.BotApi(FAKE_TOKEN, env["fake"]),
                         store.settings(discord.CHANNEL))
    bob = [m for m in store.peers(discord.CHANNEL)[0]["members"] if str(m["user_id"]) == "7"][0]
    assert bob["reachable"] is False


def test_token_never_leaves_in_errors(env):
    credentials.set_secret(discord.TOKEN_NAME, FAKE_TOKEN)
    env["fake"].error = {"__http_status": 401, "message": f"bad {FAKE_TOKEN}", "code": 0}
    out = discord.enable()
    assert FAKE_TOKEN not in json.dumps(out)


def test_status_marks_gateway_pending(env):
    _turn_on(env)
    assert discord.status()["inbound"] == "gateway_pending"


def test_discord_bridge_still_separate():
    from ops_dashboard import discord_bridge
    assert hasattr(discord_bridge, "handle_update")
    assert discord.TOKEN_NAME == "channel.discord.bot_token"


def test_credentials_refuse_a_bad_discord_token_shape(env):
    with pytest.raises(ValueError, match="Discord bot token"):
        credentials.set_secret("channel.discord.bot_token", "not-a-token")


def test_api_lists_discord_alongside_telegram(env):
    _turn_on(env)
    # routes_channels.channels_status builds both; call modules directly
    from ops_dashboard.channels import telegram
    payload = {"telegram": telegram.status(), "discord": discord.status()}
    assert payload["discord"]["channel"] == "discord"
    assert payload["discord"]["enabled"] is True
