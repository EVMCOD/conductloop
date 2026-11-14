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

Inbound live events come from the Discord Gateway (a WebSocket from this
machine; no public webhook, no open port). The ``Handler`` accepts a
MESSAGE_CREATE payload. Tests inject Gateway payloads or a fake socket;
they never talk to Discord.

The Boardroom Discord *webhook* bridge (``discord_bridge.py``,
``DISCORD_WEBHOOK_URL``) is separate and unchanged: it is not this channel.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
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


# ── Gateway (live inbound) ───────────────────────────────────────────────

# Guilds + guild messages + DMs + message content (required to read bodies).
GATEWAY_INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
# inbound shown in status(): "off" | "connecting" | "gateway"
_inbound = "off"


class WsError(RuntimeError):
    pass


class WsClient:
    """Tiny masked WebSocket client (text, ping/pong, close). wss only."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = bytearray()

    @classmethod
    def connect(cls, url: str, *, timeout: float = 20.0) -> WsClient:
        parts = urllib.parse.urlparse(url)
        if parts.scheme != "wss" or not parts.hostname:
            raise WsError("only wss:// Gateway URLs are used")
        host = parts.hostname
        port = parts.port or 443
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.settimeout(timeout)
        sock.sendall(req.encode())
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise WsError("Gateway closed during handshake")
            header += chunk
        head, _, rest = header.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
        if " 101 " not in status:
            sock.close()
            raise WsError(f"Gateway handshake failed: {status[:80]}")
        expect = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        headers = {k.lower(): v.strip() for k, _, v in
                   (line.partition(":") for line in head.decode("latin1", "replace").split("\r\n")[1:])}
        if headers.get("sec-websocket-accept") != expect:
            sock.close()
            raise WsError("Gateway handshake accept mismatch")
        client = cls(sock)
        client._buf.extend(rest)
        return client

    def send(self, text: str) -> None:
        data = (text or "").encode()
        header = bytearray()
        header.append(0x81)  # fin + text
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._sock.sendall(header + masked)

    def recv(self, timeout: float) -> str | None:
        """Return one text message, or None on timeout. Pings are answered."""
        self._sock.settimeout(timeout)
        while True:
            try:
                frame = self._read_frame()
            except TimeoutError:
                return None
            opcode, data = frame
            if opcode == 0x9:  # ping
                self._send_raw(0xA, data)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:
                raise WsError("Gateway closed the socket")
            if opcode == 0x1:
                return data.decode("utf-8", "replace")
            if opcode == 0x2:
                continue  # ignore binary

    def _send_raw(self, opcode: int, data: bytes) -> None:
        header = bytearray()
        header.append(0x80 | (opcode & 0xF))
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._sock.sendall(header + masked)

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WsError("Gateway closed the socket")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _read_frame(self) -> tuple[int, bytes]:
        b1, b2 = self._read_exact(2)
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        data = self._read_exact(length)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return opcode, data

    def close(self) -> None:
        try:
            self._send_raw(0x8, b"")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


class Gateway:
    """One Gateway session. ``socket_factory(url) -> WsClient`` is injected in tests."""

    def __init__(self, bot_token: str, handler: Handler, *,
                 socket_factory: Callable[[str], WsClient] | None = None,
                 stop: threading.Event | None = None):
        self._token = bot_token
        self.handler = handler
        self._socket_factory = socket_factory
        self._stop = stop or threading.Event()
        self.seq: int | None = None
        self.session_id: str | None = None
        self.resume_url: str | None = None
        self.connected = False
        self.sent: list[dict] = []
        self._heartbeat = 0.0
        self._last_beat = 0.0

    def _scrub(self, text: str) -> str:
        return (text or "").replace(self._token, "<token>")

    def identify_payload(self) -> dict:
        return {"op": 2, "d": {
            "token": self._token,
            "intents": GATEWAY_INTENTS,
            "properties": {"os": sys.platform, "browser": "conductloop",
                           "device": "conductloop"},
        }}

    def resume_payload(self) -> dict:
        return {"op": 6, "d": {
            "token": self._token, "session_id": self.session_id, "seq": self.seq}}

    def dispatch(self, payload: dict) -> str:
        """Handle one Gateway JSON payload. Returns a short outcome (for tests)."""
        if not isinstance(payload, dict):
            return "ignored"
        op = payload.get("op")
        seq = payload.get("s")
        if isinstance(seq, int):
            self.seq = seq
        t = payload.get("t")
        raw_d = payload.get("d")
        d = raw_d if isinstance(raw_d, dict) else {}
        if op == 10:  # Hello
            interval = float(d.get("heartbeat_interval") or 45000) / 1000.0
            self._heartbeat = max(interval, 1.0)
            self._last_beat = 0.0
            return "hello"
        if op == 11:
            return "heartbeat_ack"
        if op == 7:
            return "reconnect"
        if op == 9:
            # d is a boolean: False means do not resume.
            if raw_d is False:
                self.session_id = None
            return "invalid_session"
        if op == 0 and t == "READY":
            self.session_id = d.get("session_id")
            self.resume_url = d.get("resume_gateway_url")
            self.connected = True
            return "ready"
        if op == 0 and t == "RESUMED":
            self.connected = True
            return "resumed"
        if op == 0 and t == "MESSAGE_CREATE":
            try:
                return self.handler.handle(d)
            except Exception:
                log.exception("discord gateway message failed")
                return "error"
        return "ignored"

    def _send(self, sock: WsClient, payload: dict) -> None:
        self.sent.append({"op": payload.get("op")})  # never store the token
        sock.send(json.dumps(payload))

    def run(self, url: str = GATEWAY_URL) -> str:
        """Connect, identify or resume, then read until stop/error/reconnect.

        Returns why the session ended (``stopped``, ``reconnect``, ``error``…).
        """
        global _inbound
        factory = self._socket_factory or WsClient.connect
        sock = factory(url)
        try:
            while not self._stop.is_set():
                timeout = 1.0
                if self._heartbeat:
                    due = self._heartbeat * 0.9
                    elapsed = time.time() - self._last_beat if self._last_beat else due
                    timeout = max(0.2, min(1.0, due - elapsed if self._last_beat else 0.2))
                raw = sock.recv(timeout)
                if raw is None:
                    if self._heartbeat and (not self._last_beat or
                                            time.time() - self._last_beat >= self._heartbeat * 0.9):
                        self._send(sock, {"op": 1, "d": self.seq})
                        self._last_beat = time.time()
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                outcome = self.dispatch(payload)
                if outcome == "hello":
                    if self.session_id and self.seq is not None:
                        self._send(sock, self.resume_payload())
                    else:
                        self._send(sock, self.identify_payload())
                    _inbound = "connecting"
                elif outcome == "ready" or outcome == "resumed":
                    _inbound = "gateway"
                    store.update(CHANNEL, last_error=None, last_ok_at=int(time.time()))
                elif outcome in ("reconnect", "invalid_session"):
                    return outcome
        except (WsError, DiscordError, OSError) as e:
            log.warning("discord gateway: %s", self._scrub(str(e)))
            raise DiscordError(None, self._scrub(str(e))) from None
        finally:
            try:
                sock.close()
            except Exception:
                pass
            self.connected = False
            if _inbound == "gateway":
                _inbound = "connecting"
        return "stopped"


class Poller:
    """Background worker: run-result DMs, and a Gateway for live inbound."""

    def __init__(self, *, transport: Transport | None = None,
                 wake: Callable[[int, str], dict] = _default_wake,
                 holding_ceo: Callable[[str], str] = _default_holding_ceo,
                 socket_factory: Callable[[str], WsClient] | None = None):
        self._transport = transport
        self._wake = wake
        self._holding_ceo = holding_ceo
        self._socket_factory = socket_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._gw_thread: threading.Thread | None = None
        self._jobs: list[Callable[[], None]] = []
        self._jobs_cv = threading.Condition()
        self._worker: threading.Thread | None = None
        self._handler: Handler | None = None
        self._handler_token: str | None = None
        self._backoff = 0.0

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
        self._gw_thread = threading.Thread(target=self._gateway_loop, daemon=True,
                                           name="discord-gateway")
        self._gw_thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._jobs_cv:
            self._jobs_cv.notify_all()

    def _loop(self) -> None:
        while not self._stop.is_set():
            wait = self.run_once()
            if wait:
                self._stop.wait(wait)

    def _ensure_handler(self, bot_token: str) -> Handler:
        if self._handler is None or self._handler_token != bot_token:
            self._handler = Handler(BotApi(bot_token, self._transport), wake=self._wake,
                                    holding_ceo=self._holding_ceo, submit=self._submit)
            self._handler_token = bot_token
        return self._handler

    def run_once(self) -> float:
        """Push run results if the channel is on. Returns how long to wait."""
        global _inbound
        try:
            s = store.settings(CHANNEL)
        except Exception:
            return IDLE_WAIT
        if not s.get("enabled"):
            if _inbound != "off":
                _inbound = "off"
            return IDLE_WAIT
        bot_token = token()
        if not bot_token:
            store.update(CHANNEL, last_error="No bot token: put one in Credentials → Channels.")
            return IDLE_WAIT
        self._ensure_handler(bot_token)
        try:
            push_results(self._handler.api, s)  # type: ignore[union-attr]
            store.update(CHANNEL, last_error=None, last_ok_at=int(time.time()))
        except Exception:
            log.exception("discord run results not sent")
        return IDLE_WAIT

    def _gateway_loop(self) -> None:
        while not self._stop.is_set():
            wait = self.gateway_once()
            if wait:
                self._stop.wait(wait)

    def gateway_once(self) -> float:
        """Connect to the Gateway if the channel is on. Returns how long to wait."""
        global _inbound
        try:
            s = store.settings(CHANNEL)
        except Exception:
            _inbound = "off"
            return IDLE_WAIT
        if not s.get("enabled"):
            _inbound = "off"
            return IDLE_WAIT
        bot_token = token()
        if not bot_token:
            _inbound = "off"
            store.update(CHANNEL, last_error="No bot token: put one in Credentials → Channels.")
            return IDLE_WAIT
        if self._socket_factory is None and os.environ.get("OPS_DISABLE_CHANNELS") == "1":
            _inbound = "off"
            return IDLE_WAIT
        handler = self._ensure_handler(bot_token)
        _inbound = "connecting"
        gw = Gateway(bot_token, handler, socket_factory=self._socket_factory, stop=self._stop)
        try:
            outcome = gw.run()
        except DiscordError as e:
            self._backoff = min(max(self._backoff * 2, 2.0), 60.0)
            store.update(CHANNEL, last_error=_describe(e))
            _inbound = "connecting"
            return self._backoff
        self._backoff = 0.0
        if outcome == "stopped":
            return 0.0
        return 1.0 if outcome in ("reconnect", "invalid_session") else IDLE_WAIT


def status() -> dict:
    s = store.settings(CHANNEL)
    pairing = bool(s.get("pairing_hash")) and int(s.get("pairing_expires_at") or 0) > time.time()
    inbound = "off" if not s.get("enabled") else _inbound
    if inbound == "off" and s.get("enabled"):
        inbound = "connecting"
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
        "inbound": inbound,
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
    global _inbound
    store.update(CHANNEL, enabled=0)
    store.cancel_pairing(CHANNEL)
    _inbound = "off"
    return {"ok": True, **status()}
