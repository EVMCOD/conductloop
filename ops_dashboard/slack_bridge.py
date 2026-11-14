"""Slack bridge for the Boardroom (added 2026-07-16).

Lets the chairman route CEO replies to Slack channels and route Slack
messages back into CEO threads. Mirrors the telegram_bridge / discord_bridge
pattern: outbound via webhook URL or bot token, inbound via HTTP webhook
that Slack calls (Events API).

Setup (option A — webhook URL, simplest):
  1. In Slack: Apps → Incoming Webhooks → create one for the target channel.
  2. Copy the webhook URL (https://hooks.slack.com/services/...).
  3. In the Boardroom UI: Bind channel → Slack → paste the URL + a label.
  4. Each CEO reply is posted there automatically.

Setup (option B — bot token, supports inbound):
  1. In Slack: Apps → create app → install to workspace → Bot Token.
  2. export SLACK_BOT_TOKEN='xoxb-...' (or add to .env).
  3. For inbound, export SLACK_SIGNING_SECRET (Basic Information → Signing
     Secret) and point Event Subscriptions to <your-public-host>/api/slack/webhook.
     Inbound is fail-closed: without SLACK_SIGNING_SECRET every request is
     rejected (503); with it, X-Slack-Signature must verify over the raw body
     and the timestamp must be < 5 min old. ?secret=... (SLACK_WEBHOOK_SECRET)
     is an optional extra check, never a substitute.
  4. Subscribe to message.im / message.channels bot events.

Per-CEO routing uses the existing ceo_channels table — each CEO can be
bound to multiple Slack channels/webhooks.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("SLACK_WEBHOOK_SECRET", "").strip()
LAST_DELIVERY: dict = {}


def is_configured() -> bool:
    """Slack is always usable via webhooks — no token strictly required."""
    return True  # webhooks don't need a bot token


def _post(url: str, payload: dict, timeout: float = 10.0) -> dict:
    """POST JSON to a URL. Returns parsed JSON or {ok: False, error: ...}."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "error": f"http {e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_message(channel_id: str, text: str) -> dict:
    """Post a message to Slack.

    channel_id can be:
      - A webhook URL (https://hooks.slack.com/...)
      - A channel ID (C1234567), a channel name (#general), or a user ID (U1234567)
        — when SLACK_BOT_TOKEN is configured. Otherwise we need a webhook URL.

    Returns {ok, ts?, channel?, error?}.
    """
    text = (text or "(empty)").strip()[:4000]
    if channel_id.startswith("https://hooks.slack.com/"):
        # Incoming Webhook — direct POST.
        out = _post(channel_id, {"text": text})
        ok = bool(out) and out.get("ok") is not False and "error" not in out
        LAST_DELIVERY["last_send"] = {
            "channel_id": channel_id[:40] + "…",
            "via": "webhook",
            "ok": ok,
            "at": int(time.time()),
            "error": None if ok else (out.get("error") if isinstance(out, dict) else "webhook rejected"),
        }
        return {"ok": ok, "via": "webhook", "raw": out}
    if not TOKEN:
        return {
            "ok": False,
            "error": "Slack target_id must be a webhook URL unless SLACK_BOT_TOKEN is configured",
        }
    # Bot token mode — chat.postMessage to a channel ID/name/user.
    out = _post(
        "https://slack.com/api/chat.postMessage",
        {"channel": channel_id, "text": text},
    )
    ok = bool(out.get("ok"))
    LAST_DELIVERY["last_send"] = {
        "channel_id": channel_id,
        "via": "bot",
        "ok": ok,
        "at": int(time.time()),
        "ts": out.get("ts"),
        "error": None if ok else out.get("error"),
    }
    return out


def status() -> dict:
    """Snapshot of bridge state. Safe to call without config."""
    return {
        "configured": is_configured(),
        "bot_token_set": bool(TOKEN),
        "bot_token_prefix": (TOKEN[:10] + "…") if TOKEN else None,
        "webhook_secret_configured": bool(WEBHOOK_SECRET),
        "signing_secret_configured": inbound_enabled(),
        "inbound": "enabled" if inbound_enabled() else "disabled (set SLACK_SIGNING_SECRET)",
        "last_delivery": LAST_DELIVERY,
        "modes": ["webhook (no token needed)"] if not TOKEN else ["webhook", "bot (chat.postMessage)"],
    }


# ── Inbound: Events API webhook ────────────────────────────────────────────────


MAX_SKEW_SECONDS = 5 * 60   # Slack's recommended replay window


def _signing_secret() -> str:
    # Read at call time so setting/unsetting the env var takes effect without
    # re-importing the module.
    return os.environ.get("SLACK_SIGNING_SECRET", "").strip()


def inbound_enabled() -> bool:
    """Inbound Events API is only enabled with a signing secret configured."""
    return bool(_signing_secret())


def _authorized(request_secret: str | None) -> bool:
    """Legacy shared-secret check (``?secret=`` on the webhook URL).

    Fail closed: without ``SLACK_SIGNING_SECRET`` nothing is authorized, even
    if the legacy secret matches. With a signing secret, the legacy secret is
    an *additional* requirement only when ``SLACK_WEBHOOK_SECRET`` is set."""
    if not inbound_enabled():
        return False
    if not WEBHOOK_SECRET:
        return True
    import hmac
    return hmac.compare_digest(str(request_secret or ""), WEBHOOK_SECRET)


def authorize_request(*, body: bytes, timestamp: str | None, signature: str | None,
                      query_secret: str | None = None, now: float | None = None) -> tuple[bool, str]:
    """Decide whether an inbound Slack request may be processed.

    Returns ``(ok, reason)``. Rejects **every** request when no signing secret
    is configured (``signing_secret_not_configured``); otherwise requires a
    valid ``X-Slack-Signature`` over the raw body with a fresh
    ``X-Slack-Request-Timestamp``, plus the legacy ``?secret=`` if set."""
    if not inbound_enabled():
        return False, "signing_secret_not_configured"
    try:
        ts = int(str(timestamp or "").strip())
    except ValueError:
        return False, "bad_timestamp"
    now = time.time() if now is None else now
    if abs(now - ts) > MAX_SKEW_SECONDS:
        return False, "stale_timestamp"
    if not verify_signature(timestamp=str(ts), body=body, signature=signature or ""):
        return False, "bad_signature"
    if not _authorized(query_secret):
        return False, "bad_webhook_secret"
    return True, "ok"


def handle_event(event: dict) -> dict:
    """Process an Events API payload. Returns a dict with routing info.

    Slack sends {"type": "event_callback", "event": {...}, "team_id": ...}.
    We're interested in message events (ignore bot messages, edits, subtypes).
    """
    if event.get("type") != "event_callback":
        return {"ok": True, "ignored": event.get("type", "?")}
    inner = event.get("event") or {}
    etype = inner.get("type", "")
    if etype != "message":
        return {"ok": True, "ignored": etype}
    if inner.get("subtype"):
        return {"ok": True, "ignored": f"subtype={inner['subtype']}"}
    if inner.get("bot_id"):
        return {"ok": True, "ignored": "bot message"}

    channel = inner.get("channel", "")
    user = inner.get("user", "")
    text = inner.get("text", "")
    if not channel or not text:
        return {"ok": True, "ignored": "missing channel or text"}

    # Resolve a human-friendly author label if we can.
    author = user if user else None

    # Route into the Boardroom via ceo_channels.route_inbound.
    from . import ceo_channels as _cc
    return _cc.route_inbound("slack", channel, text, author)


def verify_signature(*args, **kwargs) -> bool:
    """HMAC-SHA256 check of Slack's ``X-Slack-Signature`` (``v0=<hex>``) over
    ``v0:<timestamp>:<raw body>``. **Fails closed**: returns False when
    ``SLACK_SIGNING_SECRET`` is not configured."""
    signing_secret = _signing_secret()
    if not signing_secret:
        return False
    timestamp = str(kwargs.get("timestamp") or "")
    body = kwargs.get("body") or b""
    if isinstance(body, str):
        body = body.encode("utf-8")
    sig = str(kwargs.get("signature") or "")
    if not timestamp or not sig:
        return False
    import hashlib
    import hmac
    base = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", sig)
