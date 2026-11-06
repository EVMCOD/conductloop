"""Telegram channel: talk to an agent from Telegram, and nobody else can.

Every test talks to a fake Telegram (no network, no real bot, no real
agent). What is protected: the channel is off by default and silent while
off; only paired private chats reach an agent; the token never shows up in
a status, an error or the database; each update is answered once.
"""
from __future__ import annotations

import json
import uuid

import pytest

from ops_dashboard import credentials, db, secret_vault
from ops_dashboard.channels import store, telegram
from ops_dashboard.routing import router

# Built at runtime so no token-shaped literal sits in the repository.
TOKEN = "1234" + "56789:" + "fakefakefake" * 3


class FakeTelegram:
    """The Bot API, in memory. ``updates`` is served once by getUpdates."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.error: dict | None = None
        self.raise_exc: Exception | None = None

    def __call__(self, url: str, payload: dict, timeout: float) -> dict:
        assert url.startswith("https://api.telegram.org/bot")
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, payload))
        if self.raise_exc:
            raise self.raise_exc
        if self.error:
            return {"ok": False, **self.error}
        if method == "getMe":
            return {"ok": True, "result": {"id": 1, "username": "my_cl_bot"}}
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return {"ok": True, "result": [u for u in out if u["update_id"] >= payload["offset"]]}
        return {"ok": True, "result": {}}

    def sent(self) -> list[tuple[object, str]]:
        return [(p["chat_id"], p["text"]) for m, p in self.calls if m == "sendMessage"]


def _msg(update_id: int, chat_id: int, text: str, chat_type: str = "private") -> dict:
    return {"update_id": update_id,
            "message": {"message_id": update_id, "text": text,
                        "chat": {"id": chat_id, "type": chat_type},
                        "from": {"id": chat_id, "username": "someone"}}}


@pytest.fixture()
def env(monkeypatch):
    """Fresh channel tables, a keychain in memory, a fake Telegram, a fake agent."""
    from ops_dashboard import secrets_store

    db.init_db()
    with db.db() as conn:
        conn.execute("DELETE FROM channel_settings")
        conn.execute("DELETE FROM channel_peers")
    items: dict[str, str] = {}
    monkeypatch.setattr(secrets_store, "keychain_available", lambda: True)
    monkeypatch.setattr(secrets_store, "keychain_get", lambda name: items.get(name))
    monkeypatch.setattr(secrets_store, "keychain_set",
                        lambda name, value: items.__setitem__(name, value) or True)
    monkeypatch.setattr(secrets_store, "keychain_delete",
                        lambda name: items.pop(name, None) is not None)
    fake = FakeTelegram()
    monkeypatch.setattr(telegram, "_urllib_transport", fake)
    team = db.create_team(f"tg-{uuid.uuid4().hex[:12]}", "Telegram test team")
    agent_id = db.add_member(team, "Scout", type="ai_agent", role="research")
    wakes: list[tuple[int, str]] = []

    def wake(aid: int, prompt: str) -> dict:
        wakes.append((aid, prompt))
        return {"ok": True, "response": f"answer to: {prompt}"}

    poller = telegram.Poller(transport=fake, wake=wake)
    poller._submit = lambda fn: fn()  # answers inline, no thread
    yield {"fake": fake, "items": items, "agent_id": agent_id, "wakes": wakes, "poller": poller}
    with db.db() as conn:
        conn.execute("DELETE FROM channel_settings")
        conn.execute("DELETE FROM channel_peers")


def _turn_on(env) -> None:
    credentials.set_secret(telegram.TOKEN_NAME, TOKEN)
    assert telegram.enable()["ok"] is True


def _pair(env, chat_id: int = 42) -> None:
    code = store.new_pairing_code("telegram")["code"]
    env["fake"].updates = [_msg(1, chat_id, f"/pair {code}")]
    env["poller"].run_once()
    assert store.peer("telegram", str(chat_id)) is not None


# ── off by default, silent while off ─────────────────────────────────────

def test_off_by_default_makes_no_network_call(env):
    credentials.set_secret(telegram.TOKEN_NAME, TOKEN)
    assert telegram.status()["enabled"] is False
    assert env["poller"].run_once() == telegram.IDLE_WAIT
    assert env["fake"].calls == []


def test_on_without_a_token_makes_no_call_and_says_why(env):
    store.update("telegram", enabled=1)
    assert env["poller"].run_once() == telegram.IDLE_WAIT
    assert env["fake"].calls == []
    assert "token" in telegram.status()["last_error"].lower()


def test_kill_switch_keeps_the_workers_stopped(monkeypatch):
    from ops_dashboard import channels

    monkeypatch.setenv("OPS_DISABLE_CHANNELS", "1")
    channels._runners.clear()
    channels.start()
    assert channels._runners == {}


def test_enable_checks_the_token_with_telegram_first(env):
    credentials.set_secret(telegram.TOKEN_NAME, TOKEN)
    env["fake"].error = {"error_code": 401, "description": "Unauthorized"}
    out = telegram.enable()
    assert out["ok"] is False and out["error"] == "token_rejected"
    assert telegram.status()["enabled"] is False
    env["fake"].error = None
    assert telegram.enable()["ok"] is True
    st = telegram.status()
    assert st["enabled"] is True and st["bot_username"] == "my_cl_bot"


# ── strangers ────────────────────────────────────────────────────────────

def test_a_stranger_gets_one_fixed_sentence_and_no_agent(env):
    _turn_on(env)
    env["fake"].updates = [_msg(1, 7, "hello"), _msg(2, 7, "are you there?")]
    env["poller"].run_once()
    assert env["wakes"] == []
    assert env["fake"].sent() == [(7, telegram.STRANGER)]   # once, not twice


def test_group_chats_are_never_answered(env):
    _turn_on(env)
    _pair(env, 42)
    env["fake"].calls.clear()
    env["fake"].updates = [_msg(3, -100, "/pair ABC", "group"), _msg(4, 42, "hi", "supergroup")]
    env["poller"].run_once()
    assert env["fake"].sent() == [] and env["wakes"] == []


# ── pairing ──────────────────────────────────────────────────────────────

def test_pairing_code_is_one_use_and_only_its_hash_is_kept(env):
    _turn_on(env)
    code = store.new_pairing_code("telegram")["code"]
    with db.db() as conn:
        row = dict(conn.execute("SELECT * FROM channel_settings").fetchone())
    assert code not in json.dumps(row)
    env["fake"].updates = [_msg(1, 42, f"/pair {code.lower()}")]
    env["poller"].run_once()
    assert store.peer("telegram", "42")["label"] == "@someone"
    assert "Paired" in env["fake"].sent()[-1][1]
    env["fake"].updates = [_msg(2, 43, f"/pair {code}")]       # second use
    env["poller"].run_once()
    assert store.peer("telegram", "43") is None
    assert "not valid" in env["fake"].sent()[-1][1]


def test_deep_link_start_with_the_code_pairs_too(env):
    _turn_on(env)
    code = store.new_pairing_code("telegram")["code"]
    env["fake"].updates = [_msg(1, 42, f"/start {code}")]
    env["poller"].run_once()
    assert store.peer("telegram", "42") is not None


def test_too_many_wrong_codes_close_the_pairing(env):
    _turn_on(env)
    code = store.new_pairing_code("telegram")["code"]
    for i in range(store.PAIRING_MAX_ATTEMPTS):
        assert store.try_pair("telegram", str(100 + i), "WRONGCDE", None) is False
    assert store.pairing_open("telegram") is False
    assert store.try_pair("telegram", "42", code, None) is False


def test_an_expired_code_does_not_pair(env, monkeypatch):
    _turn_on(env)
    code = store.new_pairing_code("telegram")["code"]
    real = store._now()
    monkeypatch.setattr(store, "_now", lambda: real + store.PAIRING_TTL + 1)
    assert store.try_pair("telegram", "42", code, None) is False


# ── talking to an agent ──────────────────────────────────────────────────

def test_a_paired_chat_picks_an_agent_and_gets_its_answer(env):
    _turn_on(env)
    _pair(env, 42)
    aid = env["agent_id"]
    env["fake"].updates = [_msg(10, 42, "what's new?")]
    env["poller"].run_once()
    assert env["wakes"] == [] and "Pick an agent" in env["fake"].sent()[-1][1]

    env["fake"].updates = [_msg(11, 42, "/agents"), _msg(12, 42, f"/use {aid}"),
                           _msg(13, 42, "what's new?")]
    env["poller"].run_once()
    sent = [t for _, t in env["fake"].sent()]
    assert any(f"{aid}  Scout" in t for t in sent)
    assert any("now talking to Scout" in t for t in sent)
    assert env["wakes"] == [(aid, "what's new?")]
    assert sent[-1] == "answer to: what's new?"


def test_the_default_agent_answers_when_none_was_picked(env):
    _turn_on(env)
    _pair(env, 42)
    store.update("telegram", default_agent_id=env["agent_id"])
    env["fake"].updates = [_msg(10, 42, "ping")]
    env["poller"].run_once()
    assert env["wakes"] == [(env["agent_id"], "ping")]


def test_a_failed_wake_is_reported_not_raised(env):
    _turn_on(env)
    _pair(env, 42)
    store.update("telegram", default_agent_id=env["agent_id"])
    env["poller"]._handler = None
    env["poller"]._wake = lambda aid, p: {"ok": False, "error": "agent wake timed out"}
    env["fake"].updates = [_msg(10, 42, "slow one")]
    env["poller"].run_once()
    assert env["fake"].sent()[-1][1] == "The agent did not answer: agent wake timed out"


def test_long_answers_are_split_and_cut():
    assert len(telegram.chunks("x" * 9000)) == 3
    parts = telegram.chunks("y" * 50_000)
    assert len(parts) == telegram.MAX_CHUNKS
    assert all(len(p) <= 4096 for p in parts) and parts[-1].endswith("rest]")


def test_each_update_is_answered_once_even_if_handling_fails(env, monkeypatch):
    _turn_on(env)
    _pair(env, 42)
    store.update("telegram", default_agent_id=env["agent_id"])
    env["poller"]._handler = None
    env["poller"].run_once()                      # builds the handler

    def boom(update):
        raise RuntimeError("crash in the middle")
    monkeypatch.setattr(env["poller"]._handler, "handle", boom)
    env["fake"].updates = [_msg(50, 42, "hi")]
    env["poller"].run_once()
    assert store.settings("telegram")["update_offset"] == 51
    env["poller"].run_once()
    assert [p for m, p in env["fake"].calls if m == "getUpdates"][-1]["offset"] == 51


# ── errors, and the token never leaks ────────────────────────────────────

def test_a_webhook_conflict_is_explained_and_backs_off(env):
    _turn_on(env)
    env["fake"].error = {"error_code": 409, "description": "Conflict: can't use getUpdates "
                                                          "method while webhook is active"}
    assert env["poller"].run_once() == 60.0
    assert "webhook" in telegram.status()["last_error"]


def test_the_token_never_leaves(env):
    _turn_on(env)
    env["fake"].raise_exc = OSError(f"cannot reach https://api.telegram.org/bot{TOKEN}/getUpdates")
    env["poller"].run_once()
    st = telegram.status()
    assert st["token"] == "keychain"
    blob = json.dumps(st) + json.dumps(credentials.inventory())
    with db.db() as conn:
        for table in ("channel_settings", "channel_peers"):
            blob += json.dumps([dict(r) for r in conn.execute(f"SELECT * FROM {table}")])
    assert TOKEN not in blob and "<token>" in st["last_error"]


def test_the_token_goes_in_through_credentials(env, monkeypatch):
    row = [r for r in credentials.inventory()["rows"] if r["name"] == telegram.TOKEN_NAME]
    assert row and row[0]["group"] == "channels" and row[0]["where"] == "nowhere"
    with pytest.raises(ValueError) as e:
        credentials.set_secret(telegram.TOKEN_NAME, "not-a-token")
    assert "not-a-token" not in str(e.value)
    credentials.set_secret(telegram.TOKEN_NAME, TOKEN)
    assert env["items"][telegram.TOKEN_NAME] == TOKEN
    credentials.delete_secret(telegram.TOKEN_NAME)
    assert telegram.token() is None


def test_no_keychain_and_a_locked_vault_refuse_the_token(monkeypatch):
    from ops_dashboard import secrets_store

    monkeypatch.setattr(secrets_store, "keychain_available", lambda: False)
    secret_vault.lock()
    with pytest.raises(secret_vault.VaultLocked):
        credentials.set_secret(telegram.TOKEN_NAME, TOKEN)


# ── the routes ───────────────────────────────────────────────────────────

class _Handler:
    def __init__(self, body: dict | None = None):
        self.body = body or {}
        self.code = None
        self.out: dict = {}

    def _read_json(self):
        return self.body

    def _send(self, code, raw, ctype):
        self.code, self.out = code, json.loads(raw)


def _call(method: str, path: str, body: dict | None = None) -> _Handler:
    from ops_dashboard import routes_channels  # noqa: F401  (registers the routes)

    h = _Handler(body)
    assert router.dispatch(h, method, path) is True
    return h


def test_routes_enable_pair_choose_and_unpair(env):
    assert _call("POST", "/api/channels/telegram/pairing").code == 409     # off
    assert _call("POST", "/api/channels/telegram/enable").out["error"] == "no_token"
    credentials.set_secret(telegram.TOKEN_NAME, TOKEN)
    h = _call("POST", "/api/channels/telegram/enable")
    assert h.code == 200 and h.out["enabled"] is True
    h = _call("POST", "/api/channels/telegram/pairing")
    assert h.code == 200 and h.out["code"] in h.out["instructions"]
    assert "@my_cl_bot" in h.out["instructions"]
    assert _call("POST", "/api/channels/telegram/default-agent", {"agent_id": 999999}).code == 400
    h = _call("POST", "/api/channels/telegram/default-agent", {"agent_id": env["agent_id"]})
    assert h.out["default_agent_id"] == env["agent_id"]
    _pair(env, 42)
    h = _call("GET", "/api/channels")
    tg = h.out["channels"]["telegram"]
    assert [p["peer_id"] for p in tg["peers"]] == ["42"] and TOKEN not in json.dumps(h.out)
    assert _call("DELETE", f"/api/channels/telegram/peers/{tg['peers'][0]['id']}").code == 200
    assert _call("DELETE", "/api/channels/telegram/peers/999999").code == 404
    h = _call("POST", "/api/channels/telegram/disable")
    assert h.out["enabled"] is False and h.out["peers"] == []


def test_migration_30_can_run_twice():
    from ops_dashboard import migrations

    fn = dict((n, f) for n, _d, f in migrations.MIGRATIONS)[30]
    with db.db() as conn:
        fn(conn)
        fn(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(channel_peers)")}
    assert {"channel", "peer_id", "agent_id", "paired_at"} <= cols


def test_the_worker_threads_start_idle_and_stop(env):
    poller = telegram.Poller(transport=env["fake"], wake=lambda a, p: {"ok": True})
    poller.start()
    poller.start()                                   # twice is harmless
    poller.stop()
    poller._thread.join(5)
    poller._worker.join(5)
    assert not poller._thread.is_alive() and not poller._worker.is_alive()
    assert env["fake"].calls == []                   # off: never called Telegram
