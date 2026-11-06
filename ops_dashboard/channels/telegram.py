"""Telegram channel: talk to an agent from Telegram and get its answer back.

How it works:

* The user creates a bot with @BotFather and puts its token in the app
  (Credentials → Channels). The token goes to the keychain or the encrypted
  vault (``channel.telegram.bot_token``), never to the database, the
  configuration or a log line.
* The channel is off until the user turns it on. While it is off this module
  makes no network call at all.
* Messages are fetched by long polling (``getUpdates``) from this machine.
  Nothing is exposed to the internet: no webhook, no open port.
* Only chats the user paired from the app are answered: private chats and
  groups. Pairing is a one-time code shown in the app (valid 10 minutes,
  5 wrong guesses close it) that is sent to the bot as ``/pair <code>``.
  Any other chat gets one fixed sentence and costs nothing: no agent, no
  provider call. In a group the bot answers only a command or a message that
  mentions it (``@botname``).
* Who answers: the agent picked in the chat (``/agents``, ``/use <id>``),
  else the channel's default agent, else the Holding CEO (the member set as
  Holding CEO, or the Boardroom's Holding CEO reply). An agent is woken with
  ``scheduler._wake_agent`` (deadline, logged in ``agent_logs``).
* Run results: when a task run finishes ``done``, its result is sent to every
  paired chat, once (``channel_deliveries``). Only runs that finish after
  the channel was turned on; never errors, never approval requests.

The old Boardroom bridge (``telegram_bridge.py``, webhook) was removed on
2026-11-06. Telegram refuses polling while a webhook is set on the bot; that
case is reported as a clear error instead of retried blindly.
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

log = logging.getLogger("conductloop.channels.telegram")

CHANNEL = "telegram"
TOKEN_NAME = "channel.telegram.bot_token"
API_BASE = "https://api.telegram.org"
POLL_TIMEOUT = 25            # seconds Telegram holds a getUpdates call open
IDLE_WAIT = 5.0              # seconds between checks while the channel is off
MAX_CHUNK = 4000             # Telegram's limit is 4096 characters per message
MAX_CHUNKS = 4               # longer answers are cut, and say so
STRANGER_REPLY_EVERY = 600   # seconds: one "this bot is private" per chat

HELP = (
    "ConductLoop\n\n"
    "/agents  list the agents you can talk to\n"
    "/use <id>  talk to that agent (/use ceo: back to the Holding CEO)\n"
    "/help  this message\n\n"
    "Anything else you write goes to the agent you picked (in a group: mention me).\n"
    "Results of finished runs are sent here."
)
STRANGER = ("This bot is private to its owner's ConductLoop. To pair this chat, create a "
            "code in ConductLoop (Channels → Telegram) and send /pair <code>.")


class TelegramError(RuntimeError):
    def __init__(self, status: int | None, description: str):
        super().__init__(description)
        self.status = status
        self.description = description


def token() -> str | None:
    """The bot token from the vault, or None (not set, or the vault is locked)."""
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


Transport = Callable[[str, dict, float], dict]


def _urllib_transport(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https host)
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            body = {}
        return {"ok": False, "error_code": e.code,
                "description": body.get("description") or f"HTTP {e.code}"}


class BotApi:
    """The few Bot API methods the channel uses. The token is only ever in
    the URL of the request; every error text is scrubbed of it."""

    def __init__(self, bot_token: str, transport: Transport | None = None):
        self._token = bot_token
        self._transport = transport or _urllib_transport

    def _scrub(self, text: str) -> str:
        return (text or "").replace(self._token, "<token>")

    def call(self, method: str, payload: dict | None = None, *, timeout: float = 15.0) -> Any:
        url = f"{API_BASE}/bot{self._token}/{method}"
        try:
            out = self._transport(url, payload or {}, timeout)
        except Exception as e:
            raise TelegramError(None, self._scrub(f"{type(e).__name__}: {e}")) from None
        if not isinstance(out, dict) or not out.get("ok"):
            out = out if isinstance(out, dict) else {}
            raise TelegramError(out.get("error_code"),
                                self._scrub(str(out.get("description") or "request failed")))
        return out.get("result")

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self, offset: int, timeout: int = POLL_TIMEOUT) -> list[dict]:
        return self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                        "allowed_updates": ["message"]},
                         timeout=timeout + 10) or []

    def send(self, chat_id: int | str, text: str) -> None:
        for part in chunks(text):
            self.call("sendMessage", {"chat_id": chat_id, "text": part,
                                      "disable_web_page_preview": True})


def chunks(text: str) -> list[str]:
    text = (text or "").strip() or "(no answer)"
    parts = [text[i:i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)]
    if len(parts) > MAX_CHUNKS:
        parts = parts[:MAX_CHUNKS]
        parts[-1] = parts[-1][:MAX_CHUNK - 40] + "\n\n[cut: open ConductLoop for the rest]"
    return parts


# ── agents ───────────────────────────────────────────────────────────────

HOLDING_CEO = "Holding CEO"
GROUP_TYPES = ("group", "supergroup")
CHAT_TYPES = ("private",) + GROUP_TYPES


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
    """The team member set as Holding CEO (``boardroom_settings.holding_ceo_id``),
    when there is one and it is an agent."""
    from .. import db
    try:
        with db.db() as conn:
            row = conn.execute("SELECT holding_ceo_id FROM boardroom_settings WHERE id = 1").fetchone()
    except Exception:
        return None
    return _agent(row["holding_ceo_id"]) if row and row["holding_ceo_id"] else None


def _default_wake(agent_id: int, prompt: str) -> dict:
    from .. import scheduler
    return scheduler._wake_agent(agent_id, prompt)


def _default_holding_ceo(prompt: str) -> str:
    """The Boardroom's Holding CEO answer (same as its other chat bridges)."""
    from .. import boardroom
    return boardroom.chat_with_holding_ceo(prompt)


# ── one update ───────────────────────────────────────────────────────────

class Handler:
    """What the bot does with one incoming message. No thread, no network of
    its own: the poller gives it the API, tests give it fakes.

    Private chats and groups the user paired. In a group the bot answers only
    a command or a message that mentions it (``@botname``); everything else
    in the group is ignored. Pairing is only ever with a code from the app.
    Who answers: the agent picked with ``/use``, else the channel's default
    agent, else the Holding CEO.
    """

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

    def bot_username(self) -> str:
        """The bot's @name (without @), from the settings or one getMe."""
        if self._username is None:
            name = store.settings(CHANNEL).get("bot_username")
            if not name:
                try:
                    name = (self.api.get_me() or {}).get("username") or ""
                    if name:
                        store.update(CHANNEL, bot_username=name)
                except TelegramError:
                    name = ""
            self._username = str(name or "")
        return self._username

    def _addressed(self, text: str, chat_type: str) -> str | None:
        """The text meant for the bot, or None when a group message is not
        for it (no command, no mention, or a command for another bot)."""
        if chat_type == "private":
            return text
        me = self.bot_username().lower()
        if text.startswith("/"):
            word = text.split(None, 1)[0]
            _, at, target = word.partition("@")
            if at and target.lower() != me:
                return None                      # /cmd@otherbot
            return text
        if not me:
            return None
        pattern = re.compile(r"(?<![\w@])@" + re.escape(me) + r"\b", re.IGNORECASE)
        if not pattern.search(text):
            return None
        return re.sub(r"\s+", " ", pattern.sub(" ", text)).strip() or None

    def handle(self, update: dict) -> str:
        """Returns what happened (for the log and the tests)."""
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        chat_type = chat.get("type")
        if chat_type not in CHAT_TYPES:
            return "ignored:chat-type"           # channels never get an answer
        chat_id = chat.get("id")
        raw = (msg.get("text") or "").strip()
        if chat_id is None or not raw:
            return "ignored:empty"
        text = self._addressed(raw, chat_type)
        if text is None:
            return "ignored:not-addressed"
        peer_id = str(chat_id)
        if chat_type in GROUP_TYPES:
            label = chat.get("title") or "group"
        else:
            sender = msg.get("from") or {}
            label = ("@" + sender["username"]) if sender.get("username") else sender.get("first_name")
        known = store.peer(CHANNEL, peer_id)

        if known is None:
            word, _, arg = text.partition(" ")
            if word.split("@")[0].lower() in ("/pair", "/start") and arg.strip():
                if store.try_pair(CHANNEL, peer_id, arg.strip(), label, chat_type):
                    self.api.send(chat_id, "Paired with ConductLoop.\n\n" + HELP)
                    return "paired"
                self.api.send(chat_id, "That code is not valid. Create a new one in ConductLoop.")
                return "pair-refused"
            last = self._stranger_replied.get(peer_id, 0.0)
            if self.clock() - last >= STRANGER_REPLY_EVERY:
                self._stranger_replied[peer_id] = self.clock()
                self.api.send(chat_id, STRANGER)
                return "stranger"
            return "stranger:quiet"

        store.touch_peer(CHANNEL, peer_id)
        if text.startswith("/"):
            return self._command(chat_id, peer_id, text)
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
            except Exception as e:  # never kill the worker
                log.warning("telegram answer failed: %s", type(e).__name__)
                reply = "The agent did not answer: an internal error happened."
            try:
                self.api.send(chat_id, reply)
            except TelegramError as e:
                log.warning("telegram reply not delivered: %s", e.description)

        self.submit(_run)
        return "asked:" + ("holding-ceo" if agent is None else str(agent["id"]))

    def _who(self, peer: dict) -> dict | None:
        """The agent that answers this chat; None = the Holding CEO's reply."""
        for candidate in (peer.get("agent_id"), store.settings(CHANNEL).get("default_agent_id")):
            agent = _agent(candidate)
            if agent is not None:
                return agent
        return _holding_ceo_member()

    def _command(self, chat_id: Any, peer_id: str, text: str) -> str:
        word, _, arg = text.partition(" ")
        cmd = word.split("@")[0].lower()
        if cmd in ("/help", "/start"):
            self.api.send(chat_id, HELP)
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
            self.api.send(chat_id, "Agents:\n" + "\n".join(lines))
            return "agents"
        if cmd == "/use":
            choice = arg.strip().lower()
            if choice in ("ceo", "default", "0", "holding"):
                store.set_peer_agent(CHANNEL, peer_id, None)
                self.api.send(chat_id, f"You are now talking to the {HOLDING_CEO}.")
                return "use:default"
            try:
                agent = _agent(int(choice))
            except ValueError:
                agent = None
            if agent is None:
                self.api.send(chat_id, "No agent with that id. Try /agents.")
                return "use-refused"
            store.set_peer_agent(CHANNEL, peer_id, int(agent["id"]))
            self.api.send(chat_id, f"You are now talking to {agent.get('name') or agent['id']}.")
            return "use"
        self.api.send(chat_id, f"Unknown command {cmd}. Try /help.")
        return "unknown-command"


# ── run results ──────────────────────────────────────────────────────────

RESULT_MAX = 3500


def format_result(r: dict) -> str:
    body = (r.get("output_text") or r.get("output_summary") or "").strip() or "(no output)"
    if len(body) > RESULT_MAX:
        body = body[:RESULT_MAX].rstrip() + "\n\n[cut: open ConductLoop for the rest]"
    who = f"\nAgent: {r['agent_name']}" if r.get("agent_name") else ""
    return f"✅ Result — task #{r['task_id']}: {(r.get('title') or '').strip()[:200]}{who}\n\n{body}"


def push_results(api: BotApi, settings: dict) -> int:
    """Send the results of runs that finished ``done`` since the channel was
    turned on to every paired chat. Each result is claimed once in the
    database before it is sent: never twice, even after a restart or with a
    second process. Failed runs and approval requests are never sent."""
    since = settings.get("results_since")
    if not settings.get("enabled") or not since:
        return 0
    sent = 0
    for r in store.pending_results(CHANNEL, int(since)):
        peers = store.peers(CHANNEL)
        if not store.claim_result(CHANNEL, r["run_id"], len(peers)):
            continue
        text = format_result(r)
        for p in peers:
            try:
                api.call("sendMessage", {"chat_id": p["peer_id"], "text": text,
                                         "disable_web_page_preview": True})
                sent += 1
            except TelegramError as e:
                log.warning("telegram result %s not delivered to a chat: %s", r["run_id"],
                            e.description)
    return sent


# ── the poller ───────────────────────────────────────────────────────────

def _describe(e: TelegramError) -> str:
    if e.status in (401, 404):
        return "Telegram rejected the bot token. Put a new one in Credentials → Channels."
    if e.status == 409:
        return ("Another client is reading this bot (a webhook, or a second ConductLoop). "
                "Remove the webhook or stop the other client.")
    return f"Telegram: {e.description}"[:300]


class Poller:
    """Background worker. Idles while the channel is off or has no token."""

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
        self._backoff = 0.0

    # Answers run one at a time on their own thread, so a slow agent does
    # not stop the bot from reading (and pairing, and /agents).
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
        self._worker = threading.Thread(target=self._work, daemon=True, name="telegram-answers")
        self._worker.start()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="telegram-poller")
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
        """One poll. Returns how long to wait before the next one."""
        try:
            s = store.settings(CHANNEL)
        except Exception:
            return IDLE_WAIT                       # database not ready yet
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
        api = self._handler.api
        try:
            push_results(api, s)                   # run results only; each one once
        except Exception:
            log.exception("telegram run results not sent")
        try:
            updates = api.get_updates(int(s.get("update_offset") or 0))
        except TelegramError as e:
            self._backoff = min(max(self._backoff * 2, 2.0), 60.0)
            if e.status in (401, 404, 409):
                self._backoff = 60.0
            store.update(CHANNEL, last_error=_describe(e))
            return self._backoff
        self._backoff = 0.0
        store.update(CHANNEL, last_error=None, last_ok_at=int(time.time()))
        for upd in updates:
            uid = upd.get("update_id")
            if not isinstance(uid, int):
                continue
            # Forward first: an update is answered at most once, even if the
            # app stops in the middle (a second answer would spend twice).
            store.advance_offset(CHANNEL, uid + 1)
            try:
                outcome = self._handler.handle(upd)
                log.info("telegram update %s: %s", uid, outcome)
            except TelegramError as e:
                store.update(CHANNEL, last_error=_describe(e))
            except Exception:
                log.exception("telegram update %s failed", uid)
        return 0.0


# ── what the routes use ──────────────────────────────────────────────────

def status() -> dict:
    s = store.settings(CHANNEL)
    pairing = bool(s.get("pairing_hash")) and int(s.get("pairing_expires_at") or 0) > time.time()
    return {
        "channel": CHANNEL,
        "enabled": bool(s.get("enabled")),
        "token": token_where(),                 # where it is, never what it is
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
    }


def _default_label(agent_id: int | None) -> str:
    agent = _agent(agent_id)
    if agent is not None:
        return str(agent.get("name") or agent["id"])
    ceo = _holding_ceo_member()
    return f"{HOLDING_CEO} ({ceo.get('name')})" if ceo and ceo.get("name") else HOLDING_CEO


def enable(transport: Transport | None = None) -> dict:
    """Check the token with getMe, then turn the channel on."""
    bot_token = token()
    if not bot_token:
        return {"ok": False, "error": "no_token",
                "message": "Put the bot token in Credentials → Channels first."}
    try:
        me = BotApi(bot_token, transport).get_me() or {}
    except TelegramError as e:
        store.update(CHANNEL, last_error=_describe(e))
        return {"ok": False, "error": "token_rejected", "message": _describe(e)}
    was_on = bool(store.settings(CHANNEL).get("enabled"))
    fields: dict[str, Any] = {"enabled": 1, "bot_username": (me.get("username") or None),
                              "last_error": None}
    if not was_on:
        # Results of runs that finish from now on; nothing older is sent.
        fields["results_since"] = int(time.time())
    store.update(CHANNEL, **fields)
    return {"ok": True, **status()}


def disable() -> dict:
    store.update(CHANNEL, enabled=0)
    store.cancel_pairing(CHANNEL)
    return {"ok": True, **status()}
