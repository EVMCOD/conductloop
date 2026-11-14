"""Chat channels (Telegram, Discord): status, on/off, pairing, paired chats.

GET    /channels                              the page (strict CSP, this machine only)

GET    /api/channels                          every channel's status — never a token
POST   /api/channels/<telegram|discord>/enable|disable|pairing|default-agent
DELETE /api/channels/<telegram|discord>/peers/<id>
POST   /api/channels/<telegram|discord>/peers/<id>/members
DELETE /api/channels/<telegram|discord>/peers/<id>/members/<uid>

Bot tokens go in through Credentials (``channel.telegram.bot_token``,
``channel.discord.bot_token``).
"""
from __future__ import annotations

import json

from .channels import discord, store, telegram
from .routes_learning import PAGE_CSP, _serve_web
from .routing import router

router.add("GET", "/channels", _serve_web("channels.html", "text/html; charset=utf-8",
                                          {"Content-Security-Policy": PAGE_CSP}))
router.add("GET", "/channels.css", _serve_web("channels.css", "text/css; charset=utf-8"))


def _send(handler, code: int, obj: dict) -> None:
    handler._send(code, json.dumps(obj).encode(), "application/json")


def _err(handler, code: int, error: str, message: str) -> None:
    _send(handler, code, {"ok": False, "error": error, "message": message})


def _body(handler) -> dict:
    try:
        data = handler._read_json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


@router.get("/api/channels")
def channels_status(handler) -> None:
    agents = [{"id": a["id"], "name": a.get("name") or str(a["id"]), "role": a.get("role") or ""}
              for a in telegram._agents()]
    _send(handler, 200, {
        "ok": True,
        "channels": {"telegram": telegram.status(), "discord": discord.status()},
        "agents": agents,
    })


@router.post("/api/channels/telegram/enable")
def telegram_enable(handler) -> None:
    out = telegram.enable()
    if not out.get("ok"):
        _err(handler, 400, out["error"], out["message"])
        return
    _send(handler, 200, out)


@router.post("/api/channels/telegram/disable")
def telegram_disable(handler) -> None:
    _send(handler, 200, telegram.disable())


@router.post("/api/channels/telegram/pairing")
def telegram_pairing(handler) -> None:
    if not store.settings(telegram.CHANNEL).get("enabled"):
        _err(handler, 409, "disabled", "Turn the Telegram channel on first.")
        return
    code = store.new_pairing_code(telegram.CHANNEL)
    bot = store.settings(telegram.CHANNEL).get("bot_username")
    _send(handler, 200, {"ok": True, **code, "bot_username": bot,
                         "instructions": f"Send /pair {code['code']} to "
                                         f"{'@' + bot if bot else 'your bot'} within 10 minutes."})


@router.post("/api/channels/telegram/default-agent")
def telegram_default_agent(handler) -> None:
    raw = _body(handler).get("agent_id")
    agent_id = None
    if raw not in (None, ""):
        try:
            agent_id = int(raw)
        except (TypeError, ValueError):
            agent_id = -1
        if telegram._agent(agent_id) is None:
            _err(handler, 400, "bad_agent", "No agent with that id.")
            return
    store.update(telegram.CHANNEL, default_agent_id=agent_id)
    _send(handler, 200, {"ok": True, **telegram.status()})


@router.delete("/api/channels/telegram/peers/<peer_row>")
def telegram_unpair(handler, peer_row: str) -> None:
    try:
        row_id = int(peer_row)
    except ValueError:
        _err(handler, 400, "bad_request", "peer id must be a number")
        return
    if not store.unpair(telegram.CHANNEL, row_id):
        _err(handler, 404, "not_found", "no such paired chat")
        return
    _send(handler, 200, {"ok": True, **telegram.status()})


def _group(handler, peer_row: str) -> dict | None:
    """The paired group with that row id, or None after sending the error."""
    try:
        row_id = int(peer_row)
    except ValueError:
        _err(handler, 400, "bad_request", "peer id must be a number")
        return None
    peer = store.peer_by_row(telegram.CHANNEL, row_id)
    if peer is None:
        _err(handler, 404, "not_found", "no such paired chat")
        return None
    if peer.get("chat_type") not in telegram.GROUP_TYPES:
        _err(handler, 400, "not_a_group", "only groups have a list of members")
        return None
    return peer


@router.post("/api/channels/telegram/peers/<peer_row>/members")
def telegram_allow_member(handler, peer_row: str) -> None:
    peer = _group(handler, peer_row)
    if peer is None:
        return
    body = _body(handler)
    user_id = body.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, (str, int)) or \
            not store.allow_member(telegram.CHANNEL, peer["peer_id"], user_id,
                                   body.get("username"), by="app"):
        _err(handler, 400, "bad_user_id",
             "user_id: the person's Telegram user id (a positive number)")
        return
    _send(handler, 200, {"ok": True, **telegram.status()})


@router.delete("/api/channels/telegram/peers/<peer_row>/members/<user_id>")
def telegram_remove_member(handler, peer_row: str, user_id: str) -> None:
    peer = _group(handler, peer_row)
    if peer is None:
        return
    if not store.remove_member(telegram.CHANNEL, peer["peer_id"], user_id):
        _err(handler, 404, "not_found", "that person is not on this group's list")
        return
    _send(handler, 200, {"ok": True, **telegram.status()})


# ── Discord (same shape as Telegram; inbound Gateway is step 2) ───────────

@router.post("/api/channels/discord/enable")
def discord_enable(handler) -> None:
    out = discord.enable()
    if not out.get("ok"):
        _err(handler, 400, out["error"], out["message"])
        return
    _send(handler, 200, out)


@router.post("/api/channels/discord/disable")
def discord_disable(handler) -> None:
    _send(handler, 200, discord.disable())


@router.post("/api/channels/discord/pairing")
def discord_pairing(handler) -> None:
    if not store.settings(discord.CHANNEL).get("enabled"):
        _err(handler, 409, "disabled", "Turn the Discord channel on first.")
        return
    code = store.new_pairing_code(discord.CHANNEL)
    bot = store.settings(discord.CHANNEL).get("bot_username")
    _send(handler, 200, {"ok": True, **code, "bot_username": bot,
                         "instructions": f"Send /pair {code['code']} to "
                                         f"{'@' + bot if bot else 'your Discord bot'} within 10 minutes."})


@router.post("/api/channels/discord/default-agent")
def discord_default_agent(handler) -> None:
    raw = _body(handler).get("agent_id")
    agent_id = None
    if raw not in (None, ""):
        try:
            agent_id = int(raw)
        except (TypeError, ValueError):
            agent_id = -1
        if discord._agent(agent_id) is None:
            _err(handler, 400, "bad_agent", "No agent with that id.")
            return
    store.update(discord.CHANNEL, default_agent_id=agent_id)
    _send(handler, 200, {"ok": True, **discord.status()})


@router.delete("/api/channels/discord/peers/<peer_row>")
def discord_unpair(handler, peer_row: str) -> None:
    try:
        row_id = int(peer_row)
    except ValueError:
        _err(handler, 400, "bad_request", "peer id must be a number")
        return
    if not store.unpair(discord.CHANNEL, row_id):
        _err(handler, 404, "not_found", "no such paired chat")
        return
    _send(handler, 200, {"ok": True, **discord.status()})


def _discord_group(handler, peer_row: str) -> dict | None:
    try:
        row_id = int(peer_row)
    except ValueError:
        _err(handler, 400, "bad_request", "peer id must be a number")
        return None
    peer = store.peer_by_row(discord.CHANNEL, row_id)
    if peer is None:
        _err(handler, 404, "not_found", "no such paired chat")
        return None
    if peer.get("chat_type") not in discord.GROUP_TYPES:
        _err(handler, 400, "not_a_group", "only server channels have a list of members")
        return None
    return peer


@router.post("/api/channels/discord/peers/<peer_row>/members")
def discord_allow_member(handler, peer_row: str) -> None:
    peer = _discord_group(handler, peer_row)
    if peer is None:
        return
    body = _body(handler)
    user_id = body.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, (str, int)) or             not store.allow_member(discord.CHANNEL, peer["peer_id"], user_id,
                                   body.get("username"), by="app"):
        _err(handler, 400, "bad_user_id",
             "user_id: the person's Discord user id (a snowflake)")
        return
    _send(handler, 200, {"ok": True, **discord.status()})


@router.delete("/api/channels/discord/peers/<peer_row>/members/<user_id>")
def discord_remove_member(handler, peer_row: str, user_id: str) -> None:
    peer = _discord_group(handler, peer_row)
    if peer is None:
        return
    if not store.remove_member(discord.CHANNEL, peer["peer_id"], user_id):
        _err(handler, 404, "not_found", "that person is not on this channel's list")
        return
    _send(handler, 200, {"ok": True, **discord.status()})

