"""Telegram channel: talk to an agent from Telegram, and nobody else can.

Every test talks to a fake Telegram (no network, no real bot, no real
agent). What is protected: the channel is off by default and silent while
off; only paired private chats reach an agent; the token never shows up in
a status, an error or the database; each update is answered once.
"""
from __future__ import annotations

import json
import re
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


OWNER = 7          # the Telegram user who pairs groups in these tests


def _msg(update_id: int, chat_id: int, text: str, chat_type: str = "private",
         sender: int | None = None, username: str = "someone") -> dict:
    if sender is None:
        sender = chat_id if chat_type == "private" else OWNER
    return {"update_id": update_id,
            "message": {"message_id": update_id, "text": text,
                        "chat": {"id": chat_id, "type": chat_type},
                        "from": {"id": sender, "username": username}}}


@pytest.fixture()
def env(monkeypatch):
    """Fresh channel tables, a keychain in memory, a fake Telegram, a fake agent."""
    from ops_dashboard import secrets_store

    db.init_db()
    with db.db() as conn:
        conn.execute("DELETE FROM channel_settings")
        conn.execute("DELETE FROM channel_peers")
        conn.execute("DELETE FROM channel_members")
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

    ceo_asks: list[str] = []

    def holding_ceo(prompt: str) -> str:
        ceo_asks.append(prompt)
        return f"CEO: {prompt}"

    poller = telegram.Poller(transport=fake, wake=wake, holding_ceo=holding_ceo)
    poller._submit = lambda fn: fn()  # answers inline, no thread
    yield {"fake": fake, "items": items, "agent_id": agent_id, "wakes": wakes, "poller": poller,
           "ceo_asks": ceo_asks}
    with db.db() as conn:
        conn.execute("DELETE FROM channel_settings")
        conn.execute("DELETE FROM channel_peers")
        conn.execute("DELETE FROM channel_members")


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


def test_channels_are_never_answered(env):
    _turn_on(env)
    _pair(env, 42)
    env["fake"].calls.clear()
    env["fake"].updates = [_msg(3, -100, "/pair ABC", "channel"), _msg(4, -101, "@my_cl_bot hi", "channel")]
    env["poller"].run_once()
    assert env["fake"].sent() == [] and env["wakes"] == [] and env["ceo_asks"] == []


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
    env["poller"].run_once()            # nobody picked: the Holding CEO answers
    assert env["wakes"] == [] and env["fake"].sent()[-1][1] == "CEO: what's new?"

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


# ── step 2: groups (answer only on mention or command) ───────────────────────

def _pair_group(env, chat_id: int = -500, title: str = "Ops room", uid: int = 2) -> None:
    code = store.new_pairing_code("telegram")["code"]
    upd = _msg(uid, chat_id, f"/pair@my_cl_bot {code}", "supergroup")
    upd["message"]["chat"]["title"] = title
    env["fake"].updates = [upd]
    env["poller"].run_once()
    peer = store.peer("telegram", str(chat_id))
    assert peer is not None and peer["chat_type"] == "supergroup" and peer["label"] == title


def test_a_group_pairs_only_with_the_app_code(env):
    _turn_on(env)
    env["fake"].updates = [_msg(1, -500, "/pair WRONGCDE", "group"),
                           _msg(2, -500, "hello everyone", "group")]
    env["poller"].run_once()
    assert store.peer("telegram", "-500") is None
    assert [t for _, t in env["fake"].sent()] == ["That code is not valid. Create a new one in ConductLoop."]
    _pair_group(env, uid=3)


def test_in_a_group_only_mentions_and_commands_are_answered(env):
    _turn_on(env)
    _pair_group(env)
    env["fake"].calls.clear()
    env["fake"].updates = [
        _msg(10, -500, "chatting among humans", "supergroup"),
        _msg(11, -500, "/agents@another_bot", "supergroup"),
        _msg(12, -500, "mail me at x@my_cl_botany.example", "supergroup"),
        _msg(13, -500, "@My_CL_Bot what is blocked?", "supergroup"),
        _msg(14, -500, "/agents", "supergroup"),
    ]
    env["poller"].run_once()
    assert env["ceo_asks"] == ["what is blocked?"]
    sent = [t for _, t in env["fake"].sent()]
    assert sent[0] == "CEO: what is blocked?" and sent[1].startswith("Agents:")
    assert len(sent) == 2


def test_an_unpaired_group_is_quiet_unless_addressed(env):
    _turn_on(env)
    env["fake"].updates = [_msg(1, -600, "random talk", "group"),
                           _msg(2, -600, "@my_cl_bot hi", "group")]
    env["poller"].run_once()
    assert env["fake"].sent() == [(-600, telegram.STRANGER)]
    assert env["ceo_asks"] == [] and env["wakes"] == []


def test_the_bot_name_comes_from_get_me_when_not_saved(env):
    _turn_on(env)
    _pair_group(env)
    store.update("telegram", bot_username=None)
    env["poller"]._handler = None
    env["fake"].updates = [_msg(20, -500, "@my_cl_bot status?", "supergroup")]
    env["poller"].run_once()
    assert env["ceo_asks"] == ["status?"]
    assert store.settings("telegram")["bot_username"] == "my_cl_bot"


# ── step 2: the Holding CEO answers by default ──────────────────────────────

def test_use_overrides_and_use_ceo_goes_back(env):
    _turn_on(env)
    _pair(env, 42)
    aid = env["agent_id"]
    env["fake"].updates = [_msg(10, 42, f"/use {aid}"), _msg(11, 42, "one"),
                           _msg(12, 42, "/use ceo"), _msg(13, 42, "two"),
                           _msg(14, 42, "/agents")]
    env["poller"].run_once()
    assert env["wakes"] == [(aid, "one")] and env["ceo_asks"] == ["two"]
    agents = env["fake"].sent()[-1][1]
    assert "→ ceo  Holding CEO (default)" in agents and f"→ {aid}" not in agents


def test_the_member_set_as_holding_ceo_is_woken(env, monkeypatch):
    _turn_on(env)
    _pair(env, 42)
    ceo = db.get_member(env["agent_id"])
    monkeypatch.setattr(telegram, "_holding_ceo_member", lambda: ceo)
    env["fake"].updates = [_msg(10, 42, "plan?")]
    env["poller"].run_once()
    assert env["wakes"] == [(env["agent_id"], "plan?")] and env["ceo_asks"] == []
    assert telegram.status()["default_agent"] == "Holding CEO (Scout)"


def test_no_boardroom_settings_means_the_boardroom_reply():
    db.init_db()
    assert telegram._holding_ceo_member() is None or telegram._holding_ceo_member()["type"] == "ai_agent"


# ── step 2: run results, only results, each once ─────────────────────────────

def _run(env, status: str, text: str = "the report") -> int:
    tid = db.create_task(None, f"task {uuid.uuid4().hex[:6]}", "do it",
                         agent_member_id=env["agent_id"])
    rid = db.start_run(tid, "mock", "m")
    assert rid
    assert db.finish_run(rid, status=status, output_text=text,
                         error="boom" if status == "error" else None)
    return rid


def _results(env) -> list[tuple[object, str]]:
    return [(c, t) for c, t in env["fake"].sent() if t.startswith("✅ Result")]


def test_done_runs_are_sent_once_to_every_paired_chat(env):
    _turn_on(env)
    _pair(env, 42)
    _pair_group(env)                               # OWNER (7) is the first member
    env["fake"].calls.clear()
    rid = _run(env, "done", "Lamp found: 10 EUR")
    env["poller"].run_once()
    got = _results(env)
    # Private chat + DM to the allowed member; never the group chat itself.
    assert {str(c) for c, _ in got} == {"42", str(OWNER)} and len(got) == 2
    assert "Lamp found: 10 EUR" in got[0][1] and "Agent: Scout" in got[0][1]
    assert "-500" not in {str(c) for c, _ in got}
    env["poller"].run_once()
    env["poller"]._handler = None                  # a "restart"
    env["poller"].run_once()
    assert len(_results(env)) == 2
    with db.db() as conn:
        assert conn.execute("SELECT peers FROM channel_deliveries WHERE run_id = ?",
                            (rid,)).fetchone()[0] == 2


def test_errors_and_approval_requests_are_never_sent(env):
    _turn_on(env)
    _pair(env, 42)
    _run(env, "error")
    _run(env, "needs_approval")
    env["poller"].run_once()
    assert _results(env) == []


def test_nothing_is_sent_while_off_or_from_before_it_was_on(env):
    credentials.set_secret(telegram.TOKEN_NAME, TOKEN)
    old = _run(env, "done", "old result")
    with db.db() as conn:
        conn.execute("UPDATE runs SET finished_at = finished_at - 100 WHERE id = ?", (old,))
    env["poller"].run_once()                         # off: no call at all
    assert env["fake"].calls == []
    assert telegram.enable()["ok"]
    _pair(env, 42)
    env["poller"].run_once()
    assert _results(env) == []
    since = store.settings("telegram")["results_since"]
    assert telegram.enable()["ok"]                   # enabling again keeps the start
    assert store.settings("telegram")["results_since"] == since


def test_a_result_with_no_paired_chat_is_not_kept_for_later(env):
    _turn_on(env)
    _run(env, "done")
    env["poller"].run_once()
    _pair(env, 42)
    env["poller"].run_once()
    assert _results(env) == []


def test_long_results_are_cut_and_a_failed_chat_does_not_resend(env):
    _turn_on(env)
    _pair(env, 42)
    _pair_group(env)
    _run(env, "done", "z" * 9000)
    real = env["fake"].__call__

    def flaky(url, payload, timeout):
        # A transient failure to the private chat must not resend the run.
        if url.endswith("/sendMessage") and str(payload.get("chat_id")) == "42":
            return {"ok": False, "error_code": 403, "description": "bot was kicked"}
        return real(url, payload, timeout)
    env["poller"]._handler = None
    env["poller"]._transport = flaky
    env["poller"].run_once()
    env["poller"].run_once()
    got = _results(env)
    assert len(got) == 1 and str(got[0][0]) == str(OWNER)   # DM to the member still went
    assert len(got[0][1]) < 4096 and got[0][1].endswith("[cut: open ConductLoop for the rest]")


def test_a_second_process_cannot_send_the_same_result():
    db.init_db()
    with db.db() as conn:
        conn.execute("DELETE FROM channel_deliveries")
    assert store.claim_result("telegram", 987654, 1) is True
    assert store.claim_result("telegram", 987654, 1) is False


def test_migration_31_can_run_twice():
    from ops_dashboard import migrations

    fn = dict((n, f) for n, _d, f in migrations.MIGRATIONS)[31]
    with db.db() as conn:
        fn(conn)
        fn(conn)
        peers = {r[1] for r in conn.execute("PRAGMA table_info(channel_peers)")}
        settings = {r[1] for r in conn.execute("PRAGMA table_info(channel_settings)")}
        deliveries = {r[1] for r in conn.execute("PRAGMA table_info(channel_deliveries)")}
    assert "chat_type" in peers and "results_since" in settings
    assert {"channel", "run_id", "sent_at", "peers"} <= deliveries


# ── step 2: the old bridge is gone ───────────────────────────────────────────

def test_the_old_bridge_is_gone():
    import importlib.util
    from pathlib import Path

    from ops_dashboard import briefing, ceo_channels

    assert importlib.util.find_spec("ops_dashboard.telegram_bridge") is None
    src = (Path(__file__).resolve().parent.parent / "ops_dashboard" / "server.py").read_text()
    assert "/api/telegram/" not in src and "telegram_bridge" not in src
    assert "telegram" not in ceo_channels.PLATFORMS
    assert ceo_channels.bind_channel(1, "telegram", "123")["ok"] is False
    assert briefing.deliver.__code__.co_names.count("telegram_bridge") == 0


# ── step 2: the page ─────────────────────────────────────────────────────────

def test_the_page_is_strict_and_builds_nothing_from_html():
    from pathlib import Path

    web = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
    page = (web / "channels.html").read_text()
    js = (web / "channels.js").read_text()
    assert "<script>" not in page and "style=" not in page
    assert not re.search(r"\son[a-z]+\s*=", page)
    assert "innerHTML" not in js and "eval(" not in js
    for route in ("/api/channels", "/api/channels/telegram/", "/channels.css", "/members"):
        assert route in js or route in page


def test_page_route_sends_the_strict_policy_and_the_api_lists_agents(env):
    from ops_dashboard import routes_channels
    from ops_dashboard.routes_learning import PAGE_CSP

    class H(_Handler):
        headers_sent: dict = {}

        def _is_local(self):
            return True

        def _send(self, code, raw, ctype, headers=None):
            self.code, self.raw, self.headers_sent = code, raw, headers or {}

    h = H()
    assert router.dispatch(h, "GET", "/channels") is True
    assert h.code == 200 and h.headers_sent.get("Content-Security-Policy") == PAGE_CSP
    assert b"/channels.js" in h.raw
    out = _call("GET", "/api/channels").out
    assert {"id": env["agent_id"], "name": "Scout", "role": "research"} in out["agents"]
    assert out["channels"]["telegram"]["default_agent"] == "Holding CEO"
    assert routes_channels  # imported for its routes


# ── step 2b: who may talk in a paired group ──────────────────────────────────

def test_the_person_who_paired_a_group_is_its_first_member(env):
    _turn_on(env)
    _pair_group(env)
    members = store.members("telegram", "-500")
    assert [(m["user_id"], m["username"], m["allowed"], m["added_by"]) for m in members] == \
        [(str(OWNER), "someone", True, "pairing")]
    assert telegram.GROUP_PAIRED in env["fake"].sent()[-1][1]


def test_people_not_on_the_list_get_a_fixed_notice_and_cost_nothing(env):
    _turn_on(env)
    _pair_group(env)
    env["fake"].calls.clear()
    env["fake"].updates = [
        _msg(30, -500, "@my_cl_bot spend my tokens", "supergroup", sender=99, username="mallory"),
        _msg(31, -500, "/use 1", "supergroup", sender=99, username="mallory"),
        _msg(32, -500, "/agents", "supergroup", sender=99, username="mallory"),
    ]
    env["poller"].run_once()
    # One fixed notice for the first address; the other two stay quiet (once ever).
    assert env["fake"].sent() == [(-500, telegram.NOT_ON_LIST)]
    assert env["ceo_asks"] == [] and env["wakes"] == []
    assert store.peer("telegram", "-500")["agent_id"] is None     # /use did nothing
    seen = {m["user_id"]: m for m in store.members("telegram", "-500")}
    assert seen["99"]["allowed"] is False and seen["99"]["username"] == "mallory"
    assert seen["99"]["denied_notice_at"]
    # Unaddressed chatter is never recorded: only people who wrote to the bot.
    env["fake"].updates = [_msg(33, -500, "just talking", "supergroup", sender=98)]
    env["poller"].run_once()
    assert "98" not in {m["user_id"] for m in store.members("telegram", "-500")}


def test_bots_and_anonymous_admins_are_ignored(env):
    _turn_on(env)
    _pair_group(env)
    env["fake"].calls.clear()
    bot = _msg(40, -500, "@my_cl_bot hi", "supergroup", sender=555)
    bot["message"]["from"]["is_bot"] = True
    anon = _msg(41, -500, "@my_cl_bot hi", "supergroup")
    del anon["message"]["from"]
    env["fake"].updates = [bot, anon]
    env["poller"].run_once()
    assert env["fake"].sent() == [] and env["ceo_asks"] == []


def test_adding_and_removing_members_from_the_app(env):
    _turn_on(env)
    _pair_group(env)
    row = store.peer("telegram", "-500")["id"]
    env["fake"].updates = [_msg(50, -500, "@my_cl_bot hello", "supergroup", sender=99,
                                username="ana")]
    env["poller"].run_once()
    assert env["ceo_asks"] == []
    r = _call("POST", f"/api/channels/telegram/peers/{row}/members", {"user_id": "99"})
    assert r.code == 200 and r.out["ok"]
    m = {x["user_id"]: x for x in r.out["peers"][0]["members"]}
    assert m["99"]["allowed"] is True and m["99"]["username"] == "ana" and m["99"]["added_by"] == "app"
    env["fake"].updates = [_msg(51, -500, "@my_cl_bot hello again", "supergroup", sender=99)]
    env["poller"].run_once()
    assert env["ceo_asks"] == ["hello again"]
    r = _call("DELETE", f"/api/channels/telegram/peers/{row}/members/99")
    assert r.code == 200
    env["fake"].updates = [_msg(52, -500, "@my_cl_bot and now?", "supergroup", sender=99)]
    env["poller"].run_once()
    assert env["ceo_asks"] == ["hello again"]


def test_member_api_refuses_bad_input(env):
    _turn_on(env)
    _pair(env, 42)
    _pair_group(env)
    group = store.peer("telegram", "-500")["id"]
    private = store.peer("telegram", "42")["id"]
    for bad in ("", "abc", "-5", "0", "1" * 21, None, True):  # >20 digits (Discord snowflake max)
        assert _call("POST", f"/api/channels/telegram/peers/{group}/members",
                     {"user_id": bad}).code == 400, bad
    assert _call("POST", f"/api/channels/telegram/peers/{private}/members",
                 {"user_id": "5"}).code == 400               # private chats have no list
    assert _call("POST", "/api/channels/telegram/peers/999/members", {"user_id": "5"}).code == 404
    assert _call("DELETE", f"/api/channels/telegram/peers/{group}/members/12345").code == 404
    assert _call("POST", f"/api/channels/telegram/peers/{group}/members",
                 {"user_id": " 123 ", "username": "@Bob_1"}).code == 200
    m = {x["user_id"]: x for x in store.members("telegram", "-500")}
    assert m["123"]["username"] == "Bob_1"


def test_unpairing_a_group_forgets_its_list(env):
    _turn_on(env)
    _pair_group(env)
    row = store.peer("telegram", "-500")["id"]
    assert store.members("telegram", "-500")
    assert store.unpair("telegram", row) is True
    assert store.members("telegram", "-500") == []


def test_only_the_newest_strangers_are_kept(env):
    _turn_on(env)
    _pair_group(env)
    for i in range(store.MAX_SEEN + 5):
        store.seen_member("telegram", "-500", 1000 + i, None)
    m = store.members("telegram", "-500")
    assert sum(1 for x in m if not x["allowed"]) == store.MAX_SEEN
    assert any(x["allowed"] for x in m)                       # the list itself is never pruned


def test_private_chats_need_no_list(env):
    _turn_on(env)
    _pair(env, 42)
    env["fake"].updates = [_msg(60, 42, "hi")]
    env["poller"].run_once()
    assert env["ceo_asks"] == ["hi"]
    assert store.members("telegram", "42") == []


# ── step 2b: the env notifier and the old table are gone ─────────────────────

def test_the_env_telegram_notifier_is_gone(monkeypatch):
    from ops_dashboard import webhooks

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    assert not hasattr(webhooks, "_send_telegram")
    assert "telegram" not in webhooks.status()
    calls = []
    monkeypatch.setattr(webhooks.httpx, "post", lambda *a, **k: calls.append(a) or None)
    assert "telegram" not in webhooks.notify("x")
    assert calls == []                                    # nothing configured: no call at all


def test_migrations_drop_the_old_table_and_add_the_list(env):
    from ops_dashboard import migrations

    fns = {v: fn for v, _d, fn in migrations.MIGRATIONS}
    with db.db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS telegram_chats (id INTEGER PRIMARY KEY, chat_id INTEGER)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_telegram_chats_bound ON telegram_chats(chat_id)")
        for _ in range(2):                                # idempotent
            fns[32](conn)
            fns[33](conn)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "telegram_chats" not in names and "idx_telegram_chats_bound" not in names
    assert "channel_members" in names
    db.init_db()                                          # the schema does not bring it back
    with db.db() as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "telegram_chats" not in names

# ── step 2c: private results to allowed members, unreachable, denied notice ──

def test_group_results_go_only_to_allowed_members_by_dm(env):
    _turn_on(env)
    _pair_group(env)
    assert store.allow_member("telegram", "-500", 99, "ana", by="app")
    env["fake"].calls.clear()
    _run(env, "done", "secret report")
    env["poller"].run_once()
    got = _results(env)
    assert {str(c) for c, _ in got} == {str(OWNER), "99"}
    assert all("secret report" in t for _, t in got)
    assert "-500" not in {str(c) for c, _ in got}
    # Someone not on the list never gets a DM, even if they addressed the bot.
    env["fake"].updates = [_msg(70, -500, "@my_cl_bot hi", "supergroup", sender=55)]
    env["poller"].run_once()
    _run(env, "done", "second")
    env["fake"].calls.clear()
    env["poller"].run_once()
    assert {str(c) for c, _ in _results(env)} == {str(OWNER), "99"}


def test_unreachable_members_are_flagged_and_not_retried(env):
    _turn_on(env)
    _pair_group(env)
    store.allow_member("telegram", "-500", 99, "ana", by="app")
    real = env["fake"].__call__

    def refuse_ana(url, payload, timeout):
        if url.endswith("/sendMessage") and str(payload.get("chat_id")) == "99":
            return {"ok": False, "error_code": 403,
                    "description": "Forbidden: bot can't initiate conversation with a user"}
        return real(url, payload, timeout)

    env["poller"]._handler = None
    env["poller"]._transport = refuse_ana
    _run(env, "done", "first")
    env["poller"].run_once()
    m = {x["user_id"]: x for x in store.members("telegram", "-500")}
    assert m["99"]["reachable"] is False and "initiate" in (m["99"]["reach_error"] or "")
    assert m[str(OWNER)]["reachable"] is True
    # Status exposes the flag for the Channels page.
    peers = telegram.status()["peers"]
    group = next(p for p in peers if p["peer_id"] == "-500")
    flagged = {x["user_id"]: x for x in group["members"]}
    assert flagged["99"]["reachable"] is False

    env["fake"].calls.clear()
    _run(env, "done", "second")
    env["poller"].run_once()
    # Ana is skipped; OWNER still gets it. No endless retry to 99.
    got = _results(env)
    assert {str(c) for c, _ in got} == {str(OWNER)}
    assert not any(str(c) == "99" for c, _ in env["fake"].sent())


def test_writing_to_the_bot_privately_clears_unreachable(env):
    _turn_on(env)
    _pair_group(env)
    store.allow_member("telegram", "-500", 99, "ana", by="app")
    store.mark_reachable("telegram", "-500", 99, False, "bot can't initiate conversation")
    # Ana opens a private chat with the bot (even as a stranger).
    env["fake"].updates = [_msg(80, 99, "/start")]
    env["poller"].run_once()
    m = {x["user_id"]: x for x in store.members("telegram", "-500")}
    assert m["99"]["reachable"] is None                  # cleared → retry next push
    env["fake"].calls.clear()
    _run(env, "done", "after start")
    env["poller"].run_once()
    assert "99" in {str(c) for c, _ in _results(env)}
    assert {x["user_id"]: x for x in store.members("telegram", "-500")}["99"]["reachable"] is True


def test_denied_notice_is_sent_once_ever_per_user_and_group(env, monkeypatch):
    _turn_on(env)
    _pair_group(env)
    env["fake"].calls.clear()
    env["fake"].updates = [_msg(90, -500, "@my_cl_bot hey", "supergroup", sender=99)]
    env["poller"].run_once()
    assert env["fake"].sent() == [(-500, telegram.NOT_ON_LIST)]
    seen = {m["user_id"]: m for m in store.members("telegram", "-500")}
    assert seen["99"]["denied_notice_at"]
    env["fake"].calls.clear()
    env["fake"].updates = [_msg(91, -500, "@my_cl_bot again", "supergroup", sender=99)]
    env["poller"].run_once()
    assert env["fake"].sent() == []                      # already told: never again
    # Even a day later, still quiet — denied_notice_at is the once-ever marker.
    real_now = store._now()
    monkeypatch.setattr(store, "_now", lambda: real_now + 7 * 24 * 60 * 60)
    env["fake"].updates = [_msg(92, -500, "@my_cl_bot later", "supergroup", sender=99)]
    env["poller"].run_once()
    assert env["fake"].sent() == []
    # A different person still gets their one notice.
    env["fake"].updates = [_msg(93, -500, "@my_cl_bot hi", "supergroup", sender=88)]
    env["poller"].run_once()
    assert env["fake"].sent() == [(-500, telegram.NOT_ON_LIST)]


def test_re_pairing_a_group_keeps_its_list(env):
    _turn_on(env)
    _pair_group(env)
    store.allow_member("telegram", "-500", 99, "ana", by="app")
    before = {(m["user_id"], m["allowed"]) for m in store.members("telegram", "-500")}
    assert ("99", True) in before and (str(OWNER), True) in before
    # Pair the same group again with a fresh code: the list stays.
    _pair_group(env, uid=200)
    after = {(m["user_id"], m["allowed"]) for m in store.members("telegram", "-500")}
    assert after == before
    # Unpair deletes it (already covered); confirm the contrast.
    row = store.peer("telegram", "-500")["id"]
    store.unpair("telegram", row)
    assert store.members("telegram", "-500") == []


def test_url_alerts_and_digest_never_go_to_telegram(env, monkeypatch):
    from ops_dashboard import webhooks

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    assert set(webhooks.status()) == {"slack", "discord"}
    assert "telegram" not in webhooks.notify("alert")
    assert not hasattr(webhooks, "_send_telegram")


def test_migration_35_adds_reachability_columns(env):
    from ops_dashboard import migrations

    fn = {v: f for v, _d, f in migrations.MIGRATIONS}[35]
    with db.db() as conn:
        for _ in range(2):                                # idempotent
            fn(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(channel_members)")}
    assert {"reachable", "reach_error", "reach_at", "denied_notice_at"} <= cols
