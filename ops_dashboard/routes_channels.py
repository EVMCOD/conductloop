"""Chat channels (Telegram): status, on/off, pairing, paired chats.

GET    /api/channels                          every channel's status — never a token
POST   /api/channels/telegram/enable          check the token with Telegram, then turn on
POST   /api/channels/telegram/disable         turn off (and close any open pairing code)
POST   /api/channels/telegram/pairing         a new one-time pairing code (shown once)
POST   /api/channels/telegram/default-agent   {"agent_id": n|null}: who answers by default
DELETE /api/channels/telegram/peers/<id>      unpair a chat

The bot token itself goes in through Credentials (``channel.telegram.bot_token``).
"""
from __future__ import annotations

import json

from .channels import store, telegram
from .routing import router


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
    _send(handler, 200, {"ok": True, "channels": {"telegram": telegram.status()}})


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
