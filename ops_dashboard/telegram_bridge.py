"""Telegram bridge for the Boardroom (created 2026-07-01).

Lets the chairman talk to the Holding CEO from their phone via a regular
Telegram bot. Replies come from the same LLM provider already wired into
the dashboard (brain.resolve_provider()).

Setup:
  1. Talk to @BotFather on Telegram, run /newbot, copy the token.
     Format: <bot_id>:<token_chars> e.g. 1234567890:AAH...bunch_of_chars
  2. export TELEGRAM_BOT_TOKEN='1234:abcd...' (or add to ops-dashboard .env)
  3. Restart server, then hit  POST /api/telegram/set_webhook
     The server will tell Telegram to send updates to
     <your-public-host>/api/telegram/webhook
  4. DM the bot. It should reply using the Holding CEO synthesis.

Optional env var:
  TELEGRAM_WEBHOOK_SECRET  a random string you pass to /api/telegram/set_webhook
                           Telegram will echo it as X-Telegram-Bot-Api-Secret-Token.
                           Server only accepts requests whose header matches.

Privacy notes:
  - The bot only replies to known chat_ids stored in telegram_chats.
  - First-time senders get a 'hi, who's the chairman?' style reply and
    their chat_id is logged but NOT bound to a user yet. To bind, the
    chairman has to approve them via the Web UI (planned v1.2).
  - Messages are NOT persisted to ceo_messages, only acknowledged.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

from . import boardroom as _br
from . import db

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
LAST_DELIVERY: dict = {}


def is_configured() -> bool:
    return bool(TOKEN)


def _api(method: str, params: dict | None = None) -> dict:
    """Call a Telegram Bot API method. Returns parsed JSON."""
    if not is_configured():
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not configured"}
    qs = urllib.parse.urlencode(params or {})
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    if qs:
        url += "?" + qs
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_message(chat_id: int | str, text: str) -> dict:
    """Send a Telegram message to chat_id via the bot."""
    if not is_configured():
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not configured"}
    payload = json.dumps({
        "chat_id": chat_id,
        "text": (text or "(empty)").strip()[:4000],
    }).encode()
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read())
            LAST_DELIVERY["last_send"] = {
                "chat_id": chat_id,
                "ok": out.get("ok"),
                "at": int(time.time()),
                "text": text[:120],
            }
            return out
    except Exception as e:
        LAST_DELIVERY["last_send"] = {"ok": False, "error": str(e), "at": int(time.time())}
        return {"ok": False, "error": str(e)}


def set_webhook(public_url: str, secret: str | None = None) -> dict:
    """Tell Telegram to deliver updates to <public_url>/api/telegram/webhook."""
    if not is_configured():
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not configured"}
    full_url = public_url.rstrip("/") + "/api/telegram/webhook"
    return _api("setWebhook", {"url": full_url, **({"secret_token": secret} if secret else {})})


def delete_webhook() -> dict:
    return _api("deleteWebhook", {"drop_pending_updates": "false"})


def get_webhook_info() -> dict:
    return _api("getWebhookInfo")


def status() -> dict:
    """Snapshot of bridge state. Safe to call even when TOKEN is missing."""
    info = get_webhook_info() if is_configured() else None
    return {
        "configured": is_configured(),
        "token_prefix": (TOKEN[:6] + "…") if is_configured() else None,
        "webhook_secret_configured": bool(WEBHOOK_SECRET),
        "webhook_info": info,
        "last_delivery": LAST_DELIVERY,
        "bot_username_hint": "see @BotFather",
    }


def _authorized(request_secret_header: str | None) -> bool:
    """Check Telegram's X-Telegram-Bot-Api-Secret-Token header (if configured)."""
    if not WEBHOOK_SECRET:
        return True  # no secret configured = accept everything (local dev mode)
    return request_secret_header == WEBHOOK_SECRET


def handle_update(update: dict) -> dict:
    """Called from /api/telegram/webhook with a parsed Telegram Update object.
    Returns the message Telegram will send back (or {} if no reply)."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True, "skipped": "no message"}
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return {"ok": True, "skipped": "no text"}
    # First-seen chat_id: auto-register (bound=0 until chairman approves)
    try:
        register_inbound_chat(update)
    except Exception:
        pass
    # Slash commands
    if text.startswith("/"):
        return handle_command(chat_id, text)
    # Otherwise: Holding CEO reply
    reply = _br.chat_with_holding_ceo(text)
    LAST_DELIVERY["last_inbound"] = {
        "chat_id": chat_id,
        "user_id": msg.get("from", {}).get("id"),
        "username": msg.get("from", {}).get("username"),
        "text": text[:140],
        "at": int(time.time()),
        "replied_at": int(time.time()),
    }
    send_message(chat_id, reply)
    return {"ok": True, "replied": True, "length": len(reply)}


def register_inbound_chat(update: dict) -> dict | None:
    """Called when an inbound message arrives. Persists the chat_id (and
    any user info) so we can route messages back later. Returns the
    persisted row or None if no message."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    chat = msg.get("chat") or {}
    from_user = msg.get("from") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return None
    now = int(time.time())
    username = (from_user.get("username") or "")[:64] or None
    first_name = (from_user.get("first_name") or "")[:64] or None
    with db.get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM telegram_chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE telegram_chats SET username = ?, first_name = ?, last_seen_at = ? WHERE chat_id = ?",
                (username, first_name, now, chat_id),
            )
        else:
            conn.execute(
                "INSERT INTO telegram_chats (chat_id, username, first_name, bound, created_at, last_seen_at) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (chat_id, username, first_name, now, now),
            )
        conn.commit()
    return {"chat_id": chat_id, "username": username, "first_name": first_name}


def handle_command(chat_id: int | str, text: str) -> dict:
    """Telegram bot slash commands."""
    cmd = text.split()[0].lower().strip()
    if cmd in ("/start", "/help"):
        send_message(chat_id, (
            "👋 ConductLoop Holding CEO.\n\n"
            "Commands:\n"
            "  /inbox  - list of CEOs + status\n"
            "  /recap  - morning brief\n"
            "  /vault  - last 5 decisions\n"
            "  /portfolio  - projects + CEOs\n"
            "\nOr just type a message — the Holding CEO will answer."
        ))
    elif cmd == "/inbox":
        inbox = _br.inbox()
        lines = [f"🏛️ {len(inbox['items'])} CEOs active\n"]
        for c in inbox["items"][:10]:
            lines.append(f"  {c['status_icon']} {c['name']}  ({c['status_label']})")
        send_message(chat_id, "\n".join(lines))
    elif cmd == "/recap":
        payload = _br._recap_payload()
        syn = _br.recap_synthesis(payload)
        prefix = "[llm] " if syn.get("source") == "llm" else "[static] "
        send_message(chat_id, prefix + syn.get("text", "(no synthesis)"))
    elif cmd == "/vault":
        items = _br.vault_feed(limit=5)
        lines = []
        for it in items:
            lines.append(f"· [{it.get('kind', '?')}] {it.get('title', '')}")
        send_message(chat_id, "📚 Recent decisions:\n" + ("\n".join(lines) if lines else "(none)"))
    elif cmd == "/portfolio":
        portfolio = _br.portfolio()
        lines = []
        for p in portfolio["projects"]:
            ceo_name = p["ceo"]["name"] if p.get("ceo") else "— unassigned —"
            lines.append(f"· {p['name']} ({p['key']}) → {ceo_name}")
        k = portfolio["kpis"]
        head = f"🌐 Portfolio\n  Projects: {k['projects_total']} · With CEO: {k['projects_with_ceo']} · Audit rows: {k['projects_with_decisions']}\n\n"
        send_message(chat_id, head + ("\n".join(lines) if lines else "(no projects)"))
    elif cmd == "/ping":
        send_message(chat_id, "pong")
    else:
        send_message(chat_id, f"Unknown command: {cmd}. Try /help")
    return {"ok": True, "command": cmd}




def list_chats(bound_only: bool = False) -> list[dict]:
    """List chat_id rows that the cron can broadcast to. bound_only=1
    means we only DM owners who explicitly approved the link."""
    with db.get_connection() as conn:
        sql = "SELECT id, user_id, chat_id, username, first_name, bound, created_at, last_seen_at FROM telegram_chats"
        if bound_only:
            sql += " WHERE bound = 1"
        sql += " ORDER BY bound DESC, last_seen_at DESC"
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def bind_chat(chat_id: int, user_id: int | None) -> dict:
    """Chairman approves a chat_id for broadcast. Sets bound=1 and the user_id."""
    with db.get_connection() as conn:
        row = conn.execute("SELECT id FROM telegram_chats WHERE chat_id = ?", (chat_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "chat_id not seen yet — send /start to the bot first"}
        conn.execute(
            "UPDATE telegram_chats SET bound = 1, user_id = ? WHERE chat_id = ?",
            (user_id, chat_id),
        )
        conn.commit()
    return {"ok": True, "chat_id": chat_id, "bound": True}


def broadcast_to_bound_chats(text: str) -> dict:
    """Sends the same text to all bound telegram_chats rows. Returns {ok, sent, failed}."""
    if not is_configured():
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not configured"}
    chats = list_chats(bound_only=True)
    if not chats:
        return {"ok": True, "sent": 0, "failed": 0, "note": "no bound chats"}
    sent = 0
    failed = 0
    for ch in chats:
        out = send_message(ch["chat_id"], text)
        if out.get("ok"):
            sent += 1
        else:
            failed += 1
    return {"ok": True, "sent": sent, "failed": failed, "total": len(chats)}
