"""Discord channel: talk to an agent from Discord and get its answer back.

Step 1 of the Discord channel (gap 1 part 2). Mirrors the Telegram channel
design that is on main (#113 / #115):

* The user creates a bot in the Discord developer portal and puts its token
  in the app (Credentials → Channels). The token goes to the keychain or the
  encrypted vault (``channel.discord.bot_token``), never to the database, the
  configuration or a log line.
* The channel is off until the user turns it on. While it is off this module
  makes no network call at all.
* Only chats the user paired from the app are answered: DMs and guild text
  channels. Pairing is a one-time code shown in the app (valid 10 minutes,
  5 wrong guesses close it) that is sent to the bot as ``/pair <code>``.
  Any other chat gets one fixed sentence and costs nothing. In a guild
  channel the bot answers only a command or a message that mentions it, and
  only from the people on that channel's list (``channel_members``, managed
  on the Channels page; the person who paired is put on it). Anyone else who
  addresses the bot gets a short fixed notice (no agent, no provider call),
  once ever per person and channel.
* Who answers: the agent picked in the chat (``/agents``, ``/use <id>``),
  else the channel's default agent, else the Holding CEO (the member set as
  Holding CEO, or the Boardroom's Holding CEO reply).
* Run results: when a task run finishes ``done``, its result is sent once
  (``channel_deliveries``): to each paired DM, and privately (DM) to each
  allowed member of a paired guild channel. Only runs that finish after the
  channel was turned on; never errors, never approval requests. Members the
  bot cannot DM (Discord 403 / 50007) are flagged on the Channels page and
  not retried until they write to the bot.

Inbound live events (Discord Gateway) are step 2: this module's ``Handler``
accepts a normalised MESSAGE_CREATE payload, and the background ``Poller``
only pushes run results while the channel is on. Tests inject messages; they
never talk to Discord.

The Boardroom Discord *webhook* bridge (``discord_bridge.py``,
``DISCORD_WEBHOOK_URL``) is separate and unchanged: it is not this channel.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from . import store

log = logging.getLogger("conductloop.channels.discord")

CHANNEL = "discord"
TOKEN_NAME = "channel.discord.bot_token"
API_BASE = "https://discord.com/api/v10"
IDLE_WAIT = 5.0
MAX_CHUNK = 1900             # Discord soft limit ~2000
MAX_CHUNKS = 4
STRANGER_REPLY_EVERY = 600
NOT_ON_LIST = ("You're not on this channel's list in ConductLoop. Ask someone who is "
               "to add you on the Channels page.")

HELP = (
    "ConductLoop\n\n"
    "/agents  list the agents you can talk to\n"
    "/use <id>  talk to that agent (/use ceo: back to the Holding CEO)\n"
    "/help  this message\n\n"
    "Anything else you write goes to the agent you picked "
    "(in a server channel: mention me).\n"
    "Results of finished runs are sent here "
    "(in a server channel: privately to people on the list)."
)
GROUP_PAIRED = ("Only the people on this channel's list in ConductLoop "
                "(Channels → Discord) can talk to the agents here. You are on it; "
                "add others from the app. Results of finished runs come to you "
                "privately — open a DM with this bot once if you have not already, "
                "so it can message you.")
STRANGER = ("This bot is private to its owner's ConductLoop. To pair this chat, "
            "create a code in ConductLoop (Channels → Discord) and send /pair <code>.")

HOLDING_CEO = "Holding CEO"
# Reuse the store's Telegram chat_type values: DMs = "private", guild text
# channels = "group" (store.try_pair only accepts private|group|supergroup).
GROUP_TYPES = ("group",)
CHAT_TYPES = ("private",) + GROUP_TYPES


class DiscordError(RuntimeError):
    def __init__(self, status: int | None, description: str, *, code: int | None = None):
        super().__init__(description)
        self.status = status
        self.code = code
        self.description = description


def token() -> str | None:
    from .. import secret_vault
    try:
        value = secret_vault.get(TOKEN_NAME)
    except Exception:
        return None
    value = (value or "").strip()
    return value or None


def token_where() -> str:
    from .. import secret_vault
    try:
        return secret_vault.where(TOKEN_NAME)
    except Exception:
        return "nowhere"


Transport = Callable[[str, str, dict | None, float, dict[str, str]], dict]


def _urllib_transport(method: str, url: str, payload: dict | None, timeout: float,
                      headers: dict[str, str]) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read() or b"{}"
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
        return {"__http_status": e.code, "message": body.get("message") or f"HTTP {e.code}",
                "code": body.get("code")}


class BotApi:
    """Discord REST helpers used by the channel. The token is only ever in the
    Authorization header; every error text is scrubbed of it."""

    def __init__(self, bot_token: str, transport: Transport | None = None):
        self._token = bot_token
        self._transport = transport or _urllib_transport
        self._bot_user: dict | None = None
        self._dm_cache: dict[str, str] = {}  # user_id -> dm channel id

    def _scrub(self, text: str) -> str:
        return (text or "").replace(self._token, "<token>")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "ConductLoop-DiscordChannel (https://conductloop.app, 1)"}

    def request(self, method: str, path: str, payload: dict | None = None,
                *, timeout: float = 15.0) -> Any:
        url = f"{API_BASE}{path}"
        try:
            out = self._transport(method, url, payload, timeout, self._headers())
        except Exception as e:
            raise DiscordError(None, self._scrub(f"{type(e).__name__}: {e}")) from None
        if isinstance(out, dict) and out.get("__http_status"):
            raise DiscordError(out.get("__http_status"),
                               self._scrub(str(out.get("message") or "request failed")),
                               code=out.get("code") if isinstance(out.get("code"), int) else None)
        return out

    def get_me(self) -> dict:
        me = self.request("GET", "/users/@me")
        if isinstance(me, dict):
            self._bot_user = me
        return me if isinstance(me, dict) else {}

    def bot_user_id(self) -> str | None:
        if self._bot_user and self._bot_user.get("id"):
            return str(self._bot_user["id"])
        try:
            me = self.get_me()
        except DiscordError:
            return None
        return str(me["id"]) if me.get("id") else None

    def send(self, channel_id: int | str, text: str) -> None:
        for part in chunks(text):
            self.request("POST", f"/channels/{channel_id}/messages", {"content": part})

    def open_dm(self, user_id: str) -> str:
        """Return the DM channel id for this user (cached)."""
        uid = str(user_id)
        if uid in self._dm_cache:
            return self._dm_cache[uid]
        out = self.request("POST", "/users/@me/channels", {"recipient_id": uid})
        cid = str((out or {}).get("id") or "")
        if not cid:
            raise DiscordError(None, "could not open DM")
        self._dm_cache[uid] = cid
        return cid

    def send_dm(self, user_id: str, text: str) -> None:
        self.send(self.open_dm(user_id), text)


def chunks(text: str) -> list[str]:
    text = (text or "").strip() or "(no answer)"
    parts = [text[i:i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)]
    if len(parts) > MAX_CHUNKS:
        parts = parts[:MAX_CHUNKS]
        parts[-1] = parts[-1][:MAX_CHUNK - 40] + "\n\n[cut: open ConductLoop for the rest]"
    return parts


def _agents() -> list[dict]:
    from .. import db
    return [m for m in db.list_members() if (m.get("type") or "") == "ai_agent"]


def _agent(agent_id: int | None) -> dict | None:
    if not agent_id:
        return None
    from .. import db
    m = db.get_member(int(agent_id))
    return m if m and (m.get("type") or "") == "ai_agent" else None


def _holding_ceo_member() -> dict | None:
    from .. import db
    try:
        with db.db() as conn:
            row = conn.execute(
                "SELECT holding_ceo_id FROM boardroom_settings WHERE id = 1").fetchone()
    except Exception:
        return None
    return _agent(row["holding_ceo_id"]) if row and row["holding_ceo_id"] else None


def _default_wake(agent_id: int, prompt: str) -> dict:
    from .. import scheduler
    return scheduler._wake_agent(agent_id, prompt)


def _default_holding_ceo(prompt: str) -> str:
    from .. import boardroom
    return boardroom.chat_with_holding_ceo(prompt)


def _chat_type(msg: dict) -> str:
    """private (DM) or group (server text channel; stored as Telegram's group)."""
    if msg.get("guild_id"):
        return "group"
    return "private"


class Handler:
    """What the bot does with one normalised MESSAGE_CREATE. No thread, no
    network of its own: the poller (or a test) gives it the API."""

    def __init__(self, api: BotApi, *, wake: Callable[[int, str], dict] = _default_wake,
                 holding_ceo: Callable[[str], str] = _default_holding_ceo,
                 submit: Callable[[Callable[[], None]], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.api = api
        self.wake = wake
        self.holding_ceo = holding_ceo
        self.submit = submit or (lambda fn: fn())
        self.clock = clock
        self._stranger_replied: dict[str, float] = {}
        self._username: str | None = None
        self._bot_id: str | None = None

    def bot_username(self) -> str:
        if self._username is None:
            name = store.settings(CHANNEL).get("bot_username")
            if not name:
                try:
                    me = self.api.get_me() or {}
                    name = me.get("username") or ""
                    if name:
                        store.update(CHANNEL, bot_username=name)
                    if me.get("id"):
                        self._bot_id = str(me["id"])
                except DiscordError:
                    name = ""
            self._username = str(name or "")
        return self._username

    def bot_id(self) -> str:
        if self._bot_id:
            return self._bot_id
        bid = self.api.bot_user_id()
        self._bot_id = bid or ""
        return self._bot_id

    def _addressed(self, msg: dict, text: str, chat_type: str) -> str | None:
        if chat_type == "private":
            return text
        me_name = self.bot_username().lower()
        me_id = self.bot_id()
        if text.startswith("/"):
            # Discord text commands have no @bot suffix the way Telegram does.
            return text
        # mention via <@id> or <@!id>, or username in mentions list
        mention_ids = {str(m.get("id")) for m in (msg.get("mentions") or []) if m.get("id")}
        if me_id and me_id in mention_ids:
            cleaned = re.sub(r"<@!?" + re.escape(me_id) + r">", " ", text)
            return re.sub(r"\s+", " ", cleaned).strip() or None
        if me_name and re.search(r"(?<![\w@])@" + re.escape(me_name) + r"\b", text, re.I):
            cleaned = re.sub(r"(?<![\w@])@" + re.escape(me_name) + r"\b", " ", text, flags=re.I)
            return re.sub(r"\s+", " ", cleaned).strip() or None
        return None

    def handle(self, msg: dict) -> str:
        """Handle one MESSAGE_CREATE-shaped dict. Returns what happened."""
        author = msg.get("author") or {}
        if author.get("bot"):
            return "ignored:bot"
        chat_type = _chat_type(msg)
        if chat_type not in CHAT_TYPES:
            return "ignored:chat-type"
        channel_id = msg.get("channel_id")
        raw = (msg.get("content") or "").strip()
        if channel_id is None or not raw:
            return "ignored:empty"
        text = self._addressed(msg, raw, chat_type)
        if text is None:
            return "ignored:not-addressed"
        peer_id = str(channel_id)
        if chat_type in GROUP_TYPES:
            label = msg.get("channel_name") or f"#{peer_id}"
        else:
            label = ("@" + author["username"]) if author.get("username") else author.get("global_name")
            if author.get("id") is not None:
                store.note_user_can_receive_dm(CHANNEL, author.get("id"))
        known = store.peer(CHANNEL, peer_id)

        if known is None:
            word, _, arg = text.partition(" ")
            if word.lower() in ("/pair", "/start") and arg.strip():
                if store.try_pair(CHANNEL, peer_id, arg.strip(), label, chat_type):
                    extra = ""
                    if chat_type in GROUP_TYPES:
                        if store.allow_member(CHANNEL, peer_id, author.get("id"),
                                              author.get("username"), by="pairing"):
                            extra = "\n\n" + GROUP_PAIRED
                    self.api.send(channel_id, "Paired with ConductLoop.\n\n" + HELP + extra)
                    return "paired"
                self.api.send(channel_id, "That code is not valid. Create a new one in ConductLoop.")
                return "pair-refused"
            last = self._stranger_replied.get(peer_id, 0.0)
            if self.clock() - last >= STRANGER_REPLY_EVERY:
                self._stranger_replied[peer_id] = self.clock()
                self.api.send(channel_id, STRANGER)
                return "stranger"
            return "stranger:quiet"

        if chat_type in GROUP_TYPES:
            if author.get("id") is None:
                return "ignored:not-a-person"
            store.seen_member(CHANNEL, peer_id, author.get("id"), author.get("username"))
            if not store.member_allowed(CHANNEL, peer_id, author.get("id")):
                if store.should_send_denied_notice(CHANNEL, peer_id, author.get("id")):
                    store.record_denied_notice(CHANNEL, peer_id, author.get("id"))
                    self.api.send(channel_id, NOT_ON_LIST)
                    return "denied:not-on-list"
                return "denied:quiet"
        store.touch_peer(CHANNEL, peer_id)
        if text.startswith("/"):
            return self._command(channel_id, peer_id, text)
        agent = self._who(known)

        def _run() -> None:
            try:
                if agent is None:
                    reply = self.holding_ceo(text) or "(no answer)"
                else:
                    out = self.wake(int(agent["id"]), text)
                    if out.get("ok"):
                        reply = out.get("response") or "(no answer)"
                    else:
                        reply = "The agent did not answer: " + str(out.get("error") or "unknown error")
            except Exception:
                log.warning("discord answer failed", exc_info=True)
                reply = "The agent did not answer: an internal error happened."
            try:
                self.api.send(channel_id, reply)
            except DiscordError as e:
                log.warning("discord reply not delivered: %s", e.description)

        self.submit(_run)
        return "asked:" + ("holding-ceo" if agent is None else str(agent["id"]))

    def _who(self, peer: dict) -> dict | None:
        for candidate in (peer.get("agent_id"), store.settings(CHANNEL).get("default_agent_id")):
            agent = _agent(candidate)
            if agent is not None:
                return agent
        return _holding_ceo_member()

    def _command(self, channel_id: Any, peer_id: str, text: str) -> str:
        word, _, arg = text.partition(" ")
        cmd = word.lower()
        if cmd in ("/help", "/start"):
            self.api.send(channel_id, HELP)
            return "help"
        if cmd == "/agents":
            peer = store.peer(CHANNEL, peer_id) or {}
            current = self._who(peer)
            cur_id = current["id"] if current else None
            lines = [f"{'→ ' if cur_id is None and not peer.get('agent_id') else ''}ceo  "
                     f"{HOLDING_CEO} (default)"]
            lines += [f"{'→ ' if a['id'] == cur_id and peer.get('agent_id') else ''}{a['id']}  "
                      f"{a.get('name') or '?'}{'  (' + a['role'] + ')' if a.get('role') else ''}"
                      for a in _agents()]
            self.api.send(channel_id, "Agents:\n" + "\n".join(lines))
            return "agents"
        if cmd == "/use":
            choice = arg.strip().lower()
            if choice in ("ceo", "default", "0", "holding"):
                store.set_peer_agent(CHANNEL, peer_id, None)
                self.api.send(channel_id, f"You are now talking to the {HOLDING_CEO}.")
                return "use:default"
            try:
                agent = _agent(int(choice))
            except ValueError:
                agent = None
            if agent is None:
                self.api.send(channel_id, "No agent with that id. Try /agents.")
                return "use-refused"
            store.set_peer_agent(CHANNEL, peer_id, int(agent["id"]))
            self.api.send(channel_id, f"You are now talking to {agent.get('name') or agent['id']}.")
            return "use"
        self.api.send(channel_id, f"Unknown command {cmd}. Try /help.")
        return "unknown-command"


RESULT_MAX = 1800


def format_result(r: dict) -> str:
    body = (r.get("output_text") or r.get("output_summary") or "").strip() or "(no output)"
    if len(body) > RESULT_MAX:
        body = body[:RESULT_MAX].rstrip() + "\n\n[cut: open ConductLoop for the rest]"
    who = f"\nAgent: {r['agent_name']}" if r.get("agent_name") else ""
    return f"✅ Result — task #{r['task_id']}: {(r.get('title') or '').strip()[:200]}{who}\n\n{body}"


def _unreachable_dm(err: DiscordError) -> bool:
    """True when Discord refused a DM (user has DMs closed, blocked the bot…)."""
    if err.code == 50007:
        return True
    if err.status != 403:
        return False
    d = (err.description or "").lower()
    return any(s in d for s in ("cannot send messages", "50007", "cannot send messages to this user"))


def _result_recipients(peers: list[dict]) -> list[tuple[str, str | None, str, str]]:
    """(send_target, member_user_id_or_None, peer_id, kind) where kind is
    'channel' (send to peer_id) or 'user' (open DM to member)."""
    out: list[tuple[str, str | None, str, str]] = []
    for p in peers:
        if (p.get("chat_type") or "private") == "private":
            out.append((str(p["peer_id"]), None, str(p["peer_id"]), "channel"))
            continue
        for m in p.get("members") or []:
            if not m.get("allowed"):
                continue
            if m.get("reachable") is False:
                continue
            out.append((str(m["user_id"]), str(m["user_id"]), str(p["peer_id"]), "user"))
    return out


def push_results(api: BotApi, settings: dict) -> int:
    since = settings.get("results_since")
    if not settings.get("enabled") or not since:
        return 0
    sent = 0
    for r in store.pending_results(CHANNEL, int(since)):
        peers = store.peers(CHANNEL)
        recipients = _result_recipients(peers)
        if not store.claim_result(CHANNEL, r["run_id"], len(recipients)):
            continue
        body = format_result(r)
        for target, member_uid, peer_id, kind in recipients:
            try:
                if kind == "user":
                    api.send_dm(target, body)
                else:
                    api.send(target, body)
                if member_uid is not None:
                    store.mark_reachable(CHANNEL, peer_id, member_uid, True)
                sent += 1
            except DiscordError as e:
                if member_uid is not None and _unreachable_dm(e):
                    store.mark_reachable(CHANNEL, peer_id, member_uid, False, e.description)
                    log.warning("discord result %s: member %s unreachable: %s",
                                r["run_id"], member_uid, e.description)
                else:
                    log.warning("discord result %s not delivered to %s: %s",
                                r["run_id"], target, e.description)
    return sent


def _describe(e: DiscordError) -> str:
    if e.status in (401, 403) and e.code in (0, None, 50001):
        if e.status == 401:
            return "Discord rejected the bot token. Put a new one in Credentials → Channels."
    if e.status == 401:
        return "Discord rejected the bot token. Put a new one in Credentials → Channels."
    return f"Discord: {e.description}"[:300]


class Poller:
    """Background worker. Step 1: pushes run results while on; does not open a
    Gateway (inbound live messages are step 2). Tests call Handler.handle."""

    def __init__(self, *, transport: Transport | None = None,
                 wake: Callable[[int, str], dict] = _default_wake,
                 holding_ceo: Callable[[str], str] = _default_holding_ceo):
        self._transport = transport
        self._wake = wake
        self._holding_ceo = holding_ceo
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._jobs: list[Callable[[], None]] = []
        self._jobs_cv = threading.Condition()
        self._worker: threading.Thread | None = None
        self._handler: Handler | None = None
        self._handler_token: str | None = None

    def _submit(self, fn: Callable[[], None]) -> None:
        with self._jobs_cv:
            self._jobs.append(fn)
            self._jobs_cv.notify()

    def _work(self) -> None:
        while not self._stop.is_set():
            with self._jobs_cv:
                while not self._jobs and not self._stop.is_set():
                    self._jobs_cv.wait(1.0)
                if self._stop.is_set():
                    return
                fn = self._jobs.pop(0)
            fn()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._work, daemon=True, name="discord-answers")
        self._worker.start()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="discord-results")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._jobs_cv:
            self._jobs_cv.notify_all()

    def _loop(self) -> None:
        while not self._stop.is_set():
            wait = self.run_once()
            if wait:
                self._stop.wait(wait)

    def run_once(self) -> float:
        try:
            s = store.settings(CHANNEL)
        except Exception:
            return IDLE_WAIT
        if not s.get("enabled"):
            return IDLE_WAIT
        bot_token = token()
        if not bot_token:
            store.update(CHANNEL, last_error="No bot token: put one in Credentials → Channels.")
            return IDLE_WAIT
        if self._handler is None or self._handler_token != bot_token:
            self._handler = Handler(BotApi(bot_token, self._transport), wake=self._wake,
                                    holding_ceo=self._holding_ceo, submit=self._submit)
            self._handler_token = bot_token
        try:
            push_results(self._handler.api, s)
            store.update(CHANNEL, last_error=None, last_ok_at=int(time.time()))
        except Exception:
            log.exception("discord run results not sent")
        return IDLE_WAIT


def status() -> dict:
    s = store.settings(CHANNEL)
    pairing = bool(s.get("pairing_hash")) and int(s.get("pairing_expires_at") or 0) > time.time()
    return {
        "channel": CHANNEL,
        "enabled": bool(s.get("enabled")),
        "token": token_where(),
        "token_name": TOKEN_NAME,
        "bot_username": s.get("bot_username"),
        "default_agent_id": s.get("default_agent_id"),
        "default_agent": _default_label(s.get("default_agent_id")),
        "results_since": s.get("results_since"),
        "pairing_open": pairing,
        "pairing_expires_at": s.get("pairing_expires_at") if pairing else None,
        "peers": store.peers(CHANNEL),
        "last_error": s.get("last_error"),
        "last_ok_at": s.get("last_ok_at"),
        "inbound": "gateway_pending",  # step 2
    }


def _default_label(agent_id: int | None) -> str:
    agent = _agent(agent_id)
    if agent is not None:
        return str(agent.get("name") or agent["id"])
    ceo = _holding_ceo_member()
    return f"{HOLDING_CEO} ({ceo.get('name')})" if ceo and ceo.get("name") else HOLDING_CEO


def enable(transport: Transport | None = None) -> dict:
    bot_token = token()
    if not bot_token:
        return {"ok": False, "error": "no_token",
                "message": "Put the bot token in Credentials → Channels first."}
    try:
        me = BotApi(bot_token, transport).get_me() or {}
    except DiscordError as e:
        store.update(CHANNEL, last_error=_describe(e))
        return {"ok": False, "error": "token_rejected", "message": _describe(e)}
    was_on = bool(store.settings(CHANNEL).get("enabled"))
    fields: dict[str, Any] = {
        "enabled": 1,
        "bot_username": (me.get("username") or None),
        "last_error": None,
    }
    if not was_on:
        fields["results_since"] = int(time.time())
    store.update(CHANNEL, **fields)
    return {"ok": True, **status()}


def disable() -> dict:
    store.update(CHANNEL, enabled=0)
    store.cancel_pairing(CHANNEL)
    return {"ok": True, **status()}
