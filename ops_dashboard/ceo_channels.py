"""Per-CEO channel bindings (added 2026-07-16).

Lets each AI CEO publish outbound to a Telegram chat or Discord channel
whenever it speaks, AND receive inbound messages from those channels
as if the chairman had typed them in the thread.

Storage: ceo_channels table (see db.py).

Bridging:
  - Telegram: uses the existing bot token via telegram_bridge.send_message(chat_id, text).
    For inbound, the webhook handler should call route_inbound(ceo_id, text) which
    appends a chairman message to the CEO's thread.
  - Discord: uses the existing webhook URL pattern from discord_channels table,
    or a bot token if configured. For inbound (more complex, requires bot
    gateway), we expose a manual "/api/boardroom/ceo/<id>/channels/inbound"
    that the Discord bot gateway can POST to.
"""
from __future__ import annotations

import time

from . import boardroom as _br
from . import db

PLATFORMS = ("telegram", "discord", "slack")
DIRECTIONS = ("outbound", "inbound", "both")


# ── CRUD ───────────────────────────────────────────────────────────────────────


def list_channels(ceo_id: int) -> list[dict]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, ceo_id, platform, target_id, target_name, enabled, direction, "
            "       created_at, updated_at, last_posted_at, last_error "
            "  FROM ceo_channels WHERE ceo_id = ? ORDER BY platform, created_at",
            (ceo_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        out.append(d)
    return out


def list_channels_by_platform(platform: str) -> list[dict]:
    """All CEOs bound to a given chat/channel. Used for inbound routing."""
    if platform not in PLATFORMS:
        return []
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, ceo_id, platform, target_id, target_name, direction "
            "  FROM ceo_channels WHERE platform = ? AND enabled = 1",
            (platform,),
        ).fetchall()
    return [dict(r) for r in rows]


def bind_channel(
    ceo_id: int,
    platform: str,
    target_id: str,
    target_name: str | None = None,
    direction: str = "both",
) -> dict:
    """Bind a CEO to a channel/chat. Idempotent: re-binding same combo updates."""
    if platform not in PLATFORMS:
        return {"ok": False, "error": f"platform must be one of {PLATFORMS}"}
    if direction not in DIRECTIONS:
        return {"ok": False, "error": f"direction must be one of {DIRECTIONS}"}
    if not target_id or not target_id.strip():
        return {"ok": False, "error": "target_id required"}

    # Confirm the CEO exists
    with db.get_connection() as conn:
        ceo = conn.execute("SELECT id, name FROM team_members WHERE id = ?", (ceo_id,)).fetchone()
        if not ceo:
            return {"ok": False, "error": f"ceo_id {ceo_id} not found"}
        now = int(time.time())
        row = conn.execute(
            "SELECT id FROM ceo_channels WHERE ceo_id = ? AND platform = ? AND target_id = ?",
            (ceo_id, platform, target_id),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE ceo_channels SET target_name = ?, direction = ?, enabled = 1, "
                "       updated_at = ?, last_error = NULL WHERE id = ?",
                (target_name, direction, now, row["id"]),
            )
            action = "updated"
        else:
            conn.execute(
                "INSERT INTO ceo_channels "
                "(ceo_id, platform, target_id, target_name, enabled, direction, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                (ceo_id, platform, target_id.strip(), target_name, direction, now, now),
            )
            action = "created"
        conn.commit()
    return {"ok": True, "action": action, "ceo_id": ceo_id, "ceo_name": ceo["name"],
            "platform": platform, "target_id": target_id, "target_name": target_name,
            "direction": direction}


def unbind_channel(channel_id: int, ceo_id: int | None = None) -> dict:
    """Delete a channel binding. If ceo_id given, also scope to that CEO (defensive)."""
    with db.get_connection() as conn:
        if ceo_id is not None:
            row = conn.execute(
                "SELECT id FROM ceo_channels WHERE id = ? AND ceo_id = ?",
                (channel_id, ceo_id),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "channel not found for this CEO"}
        conn.execute("DELETE FROM ceo_channels WHERE id = ?", (channel_id,))
        conn.commit()
    return {"ok": True, "id": channel_id}


def toggle_channel(channel_id: int, enabled: bool, ceo_id: int | None = None) -> dict:
    with db.get_connection() as conn:
        if ceo_id is not None:
            row = conn.execute(
                "SELECT id, enabled FROM ceo_channels WHERE id = ? AND ceo_id = ?",
                (channel_id, ceo_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, enabled FROM ceo_channels WHERE id = ?",
                (channel_id,),
            ).fetchone()
        if not row:
            return {"ok": False, "error": "channel not found"}
        conn.execute(
            "UPDATE ceo_channels SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, int(time.time()), channel_id),
        )
        conn.commit()
    return {"ok": True, "id": channel_id, "enabled": bool(enabled)}


# ── Outbound: CEO → channel ────────────────────────────────────────────────────


def publish_outbound(ceo_id: int, text: str, *, kind: str = "ceo_message") -> dict:
    """Publish a CEO message to all its enabled outbound channels.

    Returns {ok, deliveries: [{channel_id, platform, target_id, ok, error}, ...]}
    Never raises — best-effort publish, errors are stored per-channel.
    """
    from . import discord_bridge, slack_bridge, telegram_bridge

    channels = list_channels(ceo_id)
    outbound = [c for c in channels if c["enabled"] and c["direction"] in ("outbound", "both")]
    if not outbound:
        return {"ok": True, "deliveries": [], "note": "no outbound channels bound"}

    deliveries = []
    for ch in outbound:
        try:
            if ch["platform"] == "telegram":
                out = telegram_bridge.send_message(ch["target_id"], text)
                ok = bool(out.get("ok"))
                err = None if ok else out.get("description") or out.get("error")
            elif ch["platform"] == "discord":
                out = discord_bridge.send_message(
                    content=f"**[CEO #{ceo_id}]** {text}",
                    username=ch.get("target_name") or "ConductLoop CEO",
                )
                ok = bool(out.get("ok"))
                err = None if ok else out.get("error")
            elif ch["platform"] == "slack":
                out = slack_bridge.send_message(ch["target_id"], text)
                ok = bool(out.get("ok"))
                err = None if ok else out.get("error")
            else:
                ok, err = False, f"unknown platform {ch['platform']}"
        except Exception as e:
            ok, err = False, str(e)

        # Update last_posted_at / last_error
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE ceo_channels SET last_posted_at = ?, last_error = ?, updated_at = ? "
                "WHERE id = ?",
                (int(time.time()) if ok else None, err, int(time.time()), ch["id"]),
            )
            conn.commit()

        deliveries.append({
            "channel_id": ch["id"],
            "platform": ch["platform"],
            "target_id": ch["target_id"],
            "ok": ok,
            "error": err,
        })
    return {"ok": True, "deliveries": deliveries}


# ── Inbound: channel → CEO thread ──────────────────────────────────────────────


def route_inbound(platform: str, target_id: str, text: str, author: str | None = None) -> dict:
    """A message arrived on (platform, target_id) — append as chairman msg
    to every CEO bound to that channel with direction in ('inbound', 'both').

    Returns {ok, ceos_notified: [...], skipped: [...]}.
    """
    if platform not in PLATFORMS:
        return {"ok": False, "error": f"platform must be one of {PLATFORMS}"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty text"}

    channels = list_channels_by_platform(platform)
    matches = [c for c in channels if c["target_id"] == str(target_id)
               and c["direction"] in ("inbound", "both")]
    if not matches:
        return {"ok": True, "ceos_notified": [], "note": "no CEOs bound inbound"}

    # Prepend author label so the CEO knows who said it
    body = text
    if author and author not in text:
        body = f"[{author}] {text}"

    notified = []
    for ch in matches:
        try:
            # Append a chairman message to the CEO's thread.
            out = _br.append_ceo_message(
                member_id=ch["ceo_id"],
                user_id=None,
                role="chairman",
                content=body,
            )
            notified.append({"ceo_id": ch["ceo_id"], "message_id": out.get("id") if out else None})
        except Exception as e:
            notified.append({"ceo_id": ch["ceo_id"], "error": str(e)})

    return {"ok": True, "ceos_notified": notified, "matched_channels": len(matches)}


# ── Stats ─────────────────────────────────────────────────────────────────────


def stats() -> dict:
    with db.get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM ceo_channels").fetchone()[0]
        by_platform = {
            p: conn.execute(
                "SELECT COUNT(*) FROM ceo_channels WHERE platform = ?", (p,)
            ).fetchone()[0]
            for p in PLATFORMS
        }
        enabled = conn.execute(
            "SELECT COUNT(*) FROM ceo_channels WHERE enabled = 1"
        ).fetchone()[0]
    return {"total": total, "enabled": enabled, "by_platform": by_platform}
