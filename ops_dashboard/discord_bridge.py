"""Discord bridge for the Boardroom (created 2026-07-01).

Lets the chairman keep a 1:1 chat with the Holding CEO in a Discord channel
(mobile push notifications + team visibility) plus auto-broadcasts when
something happens in the C-Suite (a CEO flips to blocked, a new CEO is
spawned, etc.).

Who answers a free-text message (same rule as the Telegram channel since
#115 / #116):

* the AI agent chosen as Holding CEO in the Boardroom
  (``boardroom_settings.holding_ceo_id``), woken like any other agent; or
* when none is set, or the member is no longer an AI agent, the Boardroom's
  own Holding CEO reply (``chat_with_holding_ceo``).

Setup (one-time):
  1. Create a webhook in your channel:
       Discord channel → ⚙ Edit Channel → Integrations → Webhooks →
       New Webhook → copy the URL
     Format: https://discord.com/api/webhooks/<id>/<token>
  2. export DISCORD_WEBHOOK_URL='***'
  3. Server auto-restarts; verify with `GET /api/discord/status`
  4. POST /api/discord/test_send to confirm the outbound channel works
  5. (Optional inbound) Set up a bot / middleware that POSTs messages
     from the channel to /api/discord/webhook. Payload shape (flat):
       {"author": "username", "content": "text", "channel_id": "..."}
     The server replies via the same outbound webhook.

Why HTTP-only vs full Discord bot:
- The server already runs in a single thread; spinning up a Discord
  websocket client keeps the bridge alive only while the dashboard is
  up. With webhooks the round-trip is plain HTTP, mirror of the
  Telegram pattern, and survives restart just fine.
- For V2 (real bot, slash commands, voice) replace this module with a
  discord.py GatewayIntents client.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from collections.abc import Callable

from . import boardroom as _br

log = logging.getLogger("conductloop.discord_bridge")

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
LAST_DELIVERY: dict = {}

# Injected in tests. Production defaults wake the chosen Holding CEO, or
# fall back to the Boardroom's own Holding CEO reply.
WakeFn = Callable[[int, str], dict]
HoldingCeoFn = Callable[[str], str]


def is_configured() -> bool:
    return bool(WEBHOOK_URL) and WEBHOOK_URL.startswith("https://discord.com/api/webhooks/")


def send_message(content: str, *, username: str = "ConductLoop Holding CEO") -> dict:
    """POST a message to the Discord channel via the configured webhook."""
    if not is_configured():
        return {"ok": False, "error": "DISCORD_WEBHOOK_URL not configured"}
    payload = json.dumps({
        "content": (content or "(empty)")[:2000],
        "username": username[:80],
    }).encode()
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            # Discord returns 204 No Content on success
            LAST_DELIVERY["last_send"] = {
                "ok": True,
                "status": resp.status,
                "at": int(time.time()),
                "preview": content[:120],
            }
            return {"ok": True, "status": resp.status, "body_size": len(body)}
    except Exception as e:
        LAST_DELIVERY["last_send"] = {"ok": False, "error": str(e), "at": int(time.time())}
        return {"ok": False, "error": str(e)}


def status() -> dict:
    ceo = _br.holding_ceo()
    info = {
        "configured": is_configured(),
        "webhook_host": urllib.parse.urlparse(WEBHOOK_URL).netloc if WEBHOOK_URL else None,
        "holding_ceo": ceo,
        "last_delivery": LAST_DELIVERY,
    }
    return info


def _authorized(request_token: str | None) -> bool:
    """Optional shared-secret check on inbound deliveries. Set
    DISCORD_INBOUND_TOKEN env var to enable."""
    expected = os.environ.get("DISCORD_INBOUND_TOKEN", "").strip()
    if not expected:
        return True  # local dev: accept everything (Telegram pattern)
    return request_token == expected


def _default_wake(agent_id: int, prompt: str) -> dict:
    from . import scheduler
    return scheduler._wake_agent(agent_id, prompt)


def _default_holding_ceo_reply(prompt: str) -> str:
    return _br.chat_with_holding_ceo(prompt)


_UNSET = object()


def answer_as_holding_ceo(
    text: str,
    *,
    wake: WakeFn | None = None,
    holding_ceo_reply: HoldingCeoFn | None = None,
    chosen: dict | None | object = _UNSET,
) -> tuple[str, str]:
    """Reply to a free-text Discord message as the Holding CEO.

    Returns ``(reply, how)`` where ``how`` is ``"agent:<id>"`` when the
    Boardroom's chosen Holding CEO was woken, or ``"boardroom"`` when the
    fallback ``chat_with_holding_ceo`` path ran (none chosen, or the member
    is no longer an AI agent).
    """
    wake = wake or _default_wake
    holding_ceo_reply = holding_ceo_reply or _default_holding_ceo_reply
    if chosen is _UNSET:
        chosen = _br.holding_ceo()
    if isinstance(chosen, dict) and chosen.get("id"):
        try:
            out = wake(int(chosen["id"]), text)
        except Exception as e:
            log.warning("discord holding-ceo wake failed: %s", type(e).__name__)
            return ("The agent did not answer: an internal error happened.",
                    f"agent:{chosen['id']}:error")
        if out.get("ok"):
            return (out.get("response") or "(no answer)", f"agent:{chosen['id']}")
        return ("The agent did not answer: " + str(out.get("error") or "unknown error"),
                f"agent:{chosen['id']}:error")
    return (holding_ceo_reply(text) or "(no answer)", "boardroom")


def handle_update(
    update: dict,
    *,
    wake: WakeFn | None = None,
    holding_ceo_reply: HoldingCeoFn | None = None,
    send: Callable[..., dict] | None = None,
) -> dict:
    """Called from /api/discord/webhook with a parsed update.
    Shape: {"author": "username", "content": "text", "channel_id": "..."}."""
    author = (update.get("author") or "chairman").strip()[:64]
    text = (update.get("content") or "").strip()
    channel_id = update.get("channel_id") or ""
    if not text:
        return {"ok": True, "skipped": "no content"}
    LAST_DELIVERY["last_inbound"] = {
        "author": author,
        "content": text[:140],
        "channel_id": channel_id,
        "at": int(time.time()),
    }
    # Slash-style commands reused from telegram
    if text.startswith("/"):
        return _handle_command(text, author, send=send)
    # Otherwise Holding CEO reply (chosen agent, or Boardroom fallback)
    reply, how = answer_as_holding_ceo(
        text, wake=wake, holding_ceo_reply=holding_ceo_reply)
    sender = send or send_message
    username = "ConductLoop Holding CEO"
    ceo = _br.holding_ceo() if how.startswith("agent:") else None
    if ceo and ceo.get("name"):
        username = f"{ceo['name']} (Holding CEO)"[:80]
    sender(reply, username=username)
    LAST_DELIVERY["last_inbound"]["replied"] = True
    LAST_DELIVERY["last_inbound"]["how"] = how
    return {"ok": True, "replied": True, "length": len(reply), "how": how}


def _handle_command(text: str, author: str, *, send: Callable[..., dict] | None = None) -> dict:
    sender = send or send_message
    cmd = text.split()[0].lower().strip()
    if cmd in ("/help", "/start"):
        ceo = _br.holding_ceo()
        who = (f"the chosen agent **{ceo['name']}**" if ceo
               else "the Boardroom's Holding CEO reply (no agent chosen yet)")
        sender(
            "👋 ConductLoop Holding CEO on Discord.\n\n"
            f"Free-text messages are answered by {who}.\n\n"
            "Commands:\n"
            "  /inbox  - list of CEOs + status\n"
            "  /recap  - morning brief\n"
            "  /vault  - last 5 decisions\n"
            "  /portfolio  - projects + CEOs\n"
            "\nOr just type a message — the Holding CEO will answer."
        )
    elif cmd == "/inbox":
        inbox = _br.inbox()
        lines = [f"🏛️ {len(inbox['items'])} CEOs active"]
        for c in inbox["items"][:10]:
            lines.append(f"  {c['status_icon']} {c['name']}  ({c['status_label']})")
        sender("\n".join(lines))
    elif cmd == "/recap":
        syn = _br.recap_synthesis(_br._recap_payload())
        prefix = "[llm] " if syn.get("source") == "llm" else "[static] "
        sender(prefix + syn.get("text", "(no synthesis)"))
    elif cmd == "/vault":
        items = _br.vault_feed(limit=5)
        lines = []
        for it in items:
            lines.append(f"· [{it.get('kind', '?')}] {it.get('title', '')}")
        sender("📚 Recent decisions:\n" + ("\n".join(lines) if lines else "(none)"))
    elif cmd == "/portfolio":
        p = _br.portfolio()
        lines = []
        for proj in p["projects"]:
            ceo_name = proj["ceo"]["name"] if proj.get("ceo") else "— unassigned —"
            lines.append(f"· {proj['name']} → {ceo_name}")
        k = p["kpis"]
        head = (f"🌐 Portfolio · {k['projects_total']} projects · "
                f"{k['projects_with_ceo']} with CEO · {k['projects_with_decisions']} audit rows\n\n")
        sender(head + ("\n".join(lines) if lines else "(no projects)"))
    elif cmd == "/ping":
        sender(f"pong ({author})")
    else:
        sender(f"Unknown command: {cmd}. Try /help.")
    return {"ok": True, "command": cmd}


def broadcast_status_change(member_id: int, old_status: str, new_status: str, member_name: str) -> dict:
    """Auto-broadcast when a CEO's status changes (called from append_ceo_message
    if you want to wire this up). Emojis give Discord users a quick at-a-glance."""
    if not is_configured():
        return {"ok": False, "error": "not configured"}
    if old_status == new_status:
        return {"ok": True, "no_change": True}
    icon = {"idle": "🟢", "working": "🟢", "waiting": "🟡",
            "blocked": "🔴", "paused": "⚪"}.get(new_status, "⚪")
    msg = f"{icon} **{member_name}**: {old_status} → {new_status}"
    return send_message(msg)
