"""Settings and paired peers of the chat channels (migration 30).

Nothing here is a secret. The bot token is in the vault; a pairing code is
kept only as a SHA-256 hash, only until it is used, expires, or has been
guessed wrong too often.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from .. import db

PAIRING_TTL = 10 * 60          # seconds a pairing code is valid
PAIRING_MAX_ATTEMPTS = 5       # wrong codes before the code is thrown away
# No 0/O, 1/I/L: the code is read off a screen and typed on a phone.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8


def _now() -> int:
    return int(time.time())


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def _ensure(conn, channel: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO channel_settings (channel, updated_at) VALUES (?, ?)",
        (channel, _now()))


def settings(channel: str) -> dict:
    with db.db() as conn:
        _ensure(conn, channel)
        row = conn.execute("SELECT * FROM channel_settings WHERE channel = ?",
                           (channel,)).fetchone()
    return dict(row)


def update(channel: str, **fields) -> None:
    allowed = {"enabled", "default_agent_id", "update_offset", "bot_username",
               "last_error", "last_ok_at", "results_since"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown channel setting(s): {sorted(bad)}")
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.db() as conn:
        _ensure(conn, channel)
        conn.execute(f"UPDATE channel_settings SET {cols}, updated_at = ? WHERE channel = ?",
                     (*fields.values(), _now(), channel))


def advance_offset(channel: str, offset: int) -> None:
    """Only ever forwards: an older poll answer cannot move the offset back."""
    with db.db() as conn:
        _ensure(conn, channel)
        conn.execute("UPDATE channel_settings SET update_offset = ?, updated_at = ? "
                     "WHERE channel = ? AND update_offset < ?",
                     (int(offset), _now(), channel, int(offset)))


# ── pairing ──────────────────────────────────────────────────────────────

def new_pairing_code(channel: str) -> dict:
    """A fresh one-time code. Replaces any earlier one. Shown once to the user."""
    code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))
    expires = _now() + PAIRING_TTL
    with db.db() as conn:
        _ensure(conn, channel)
        conn.execute("UPDATE channel_settings SET pairing_hash = ?, pairing_expires_at = ?, "
                     "pairing_attempts = 0, updated_at = ? WHERE channel = ?",
                     (_hash(code), expires, _now(), channel))
    return {"code": code, "expires_at": expires}


def cancel_pairing(channel: str) -> None:
    with db.db() as conn:
        _ensure(conn, channel)
        conn.execute("UPDATE channel_settings SET pairing_hash = NULL, pairing_expires_at = NULL, "
                     "pairing_attempts = 0, updated_at = ? WHERE channel = ?", (_now(), channel))


def pairing_open(channel: str) -> bool:
    s = settings(channel)
    return bool(s["pairing_hash"]) and int(s["pairing_expires_at"] or 0) > _now()


def try_pair(channel: str, peer_id: str, code: str, label: str | None,
             chat_type: str = "private") -> bool:
    """Pair ``peer_id`` when ``code`` is the open code. One use; too many
    wrong guesses close the code for everyone."""
    with db.db() as conn:
        _ensure(conn, channel)
        row = conn.execute("SELECT pairing_hash, pairing_expires_at, pairing_attempts "
                           "FROM channel_settings WHERE channel = ?", (channel,)).fetchone()
        if not row["pairing_hash"] or int(row["pairing_expires_at"] or 0) <= _now():
            return False
        if not hmac.compare_digest(row["pairing_hash"], _hash(code or "")):
            attempts = int(row["pairing_attempts"] or 0) + 1
            if attempts >= PAIRING_MAX_ATTEMPTS:
                conn.execute("UPDATE channel_settings SET pairing_hash = NULL, "
                             "pairing_expires_at = NULL, pairing_attempts = 0 WHERE channel = ?",
                             (channel,))
            else:
                conn.execute("UPDATE channel_settings SET pairing_attempts = ? WHERE channel = ?",
                             (attempts, channel))
            return False
        conn.execute("UPDATE channel_settings SET pairing_hash = NULL, pairing_expires_at = NULL, "
                     "pairing_attempts = 0, updated_at = ? WHERE channel = ?", (_now(), channel))
        conn.execute("INSERT INTO channel_peers (channel, peer_id, label, paired_at, last_seen_at, "
                     "chat_type) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(channel, peer_id) DO UPDATE "
                     "SET label = excluded.label, last_seen_at = excluded.last_seen_at, "
                     "chat_type = excluded.chat_type",
                     (channel, str(peer_id), (label or "")[:64] or None, _now(), _now(),
                      chat_type if chat_type in ("private", "group", "supergroup") else "private"))
    return True


# ── peers ────────────────────────────────────────────────────────────────

def peer(channel: str, peer_id: str) -> dict | None:
    with db.db() as conn:
        row = conn.execute("SELECT * FROM channel_peers WHERE channel = ? AND peer_id = ?",
                           (channel, str(peer_id))).fetchone()
    return dict(row) if row else None


def peers(channel: str) -> list[dict]:
    with db.db() as conn:
        rows = conn.execute("SELECT id, channel, peer_id, label, chat_type, agent_id, paired_at, "
                            "last_seen_at FROM channel_peers WHERE channel = ? ORDER BY id",
                            (channel,)).fetchall()
    return [dict(r) for r in rows]


def touch_peer(channel: str, peer_id: str) -> None:
    with db.db() as conn:
        conn.execute("UPDATE channel_peers SET last_seen_at = ? WHERE channel = ? AND peer_id = ?",
                     (_now(), channel, str(peer_id)))


def set_peer_agent(channel: str, peer_id: str, agent_id: int | None) -> None:
    with db.db() as conn:
        conn.execute("UPDATE channel_peers SET agent_id = ? WHERE channel = ? AND peer_id = ?",
                     (agent_id, channel, str(peer_id)))


def unpair(channel: str, row_id: int) -> bool:
    with db.db() as conn:
        cur = conn.execute("DELETE FROM channel_peers WHERE channel = ? AND id = ?",
                           (channel, int(row_id)))
        return cur.rowcount == 1


# ── run results (sent once) ─────────────────────────────────────────────

def pending_results(channel: str, since: int, limit: int = 5) -> list[dict]:
    """Runs that finished ``done`` after ``since`` and were not handled yet.
    Only results: failed runs and runs waiting for approval are never here."""
    with db.db() as conn:
        rows = conn.execute(
            "SELECT r.id AS run_id, r.task_id, r.output_text, r.finished_at, "
            "       t.title, t.output_summary, m.name AS agent_name "
            "  FROM runs r JOIN tasks t ON t.id = r.task_id "
            "  LEFT JOIN team_members m ON m.id = t.agent_member_id "
            " WHERE r.status = 'done' AND r.finished_at >= ? "
            "   AND NOT EXISTS (SELECT 1 FROM channel_deliveries d "
            "                    WHERE d.channel = ? AND d.run_id = r.id) "
            " ORDER BY r.finished_at, r.id LIMIT ?",
            (int(since), channel, int(limit))).fetchall()
    return [dict(r) for r in rows]


def claim_result(channel: str, run_id: int, peers: int) -> bool:
    """Mark a run result as handled. True only for the first caller, so a
    result is sent at most once even with two processes on one database."""
    with db.db() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO channel_deliveries (channel, run_id, sent_at, peers) "
                           "VALUES (?, ?, ?, ?)", (channel, int(run_id), _now(), int(peers)))
        return cur.rowcount == 1
