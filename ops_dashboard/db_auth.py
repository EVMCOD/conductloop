"""Users, auth, sessions, device tokens, and LLM usage metering.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so
``db.<name>`` callers keep working. Uses the shared transaction context manager
via ``_dbmod.db()`` (module-reference import, safe mid-import). bcrypt is
imported the same guarded way as in db.py so a missing wheel degrades to
verify_password() -> False rather than an ImportError at load.
"""
from __future__ import annotations

import secrets
import sqlite3
import time

try:
    import bcrypt  # type: ignore
    _HAS_BCRYPT = True
except ImportError:  # pragma: no cover
    bcrypt = None  # type: ignore
    _HAS_BCRYPT = False

# ============================================================
# Users + Auth + Billing
# ============================================================
import hashlib

from ops_dashboard import db as _dbmod

# ----- Users -----

def create_user(*, email: str, password: str | None = None, name: str | None = None,
                locale: str = "en", is_admin: bool = False, email_verified: bool = False) -> dict | None:
    """Create a user. Password is bcrypt-hashed. Returns the user row, or None if email exists."""
    now = int(time.time())
    password_hash = None
    if password:
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    verification_token = secrets.token_urlsafe(32) if not email_verified else None
    try:
        with _dbmod.db() as conn:
            cur = conn.execute(
                """INSERT INTO users (email, password_hash, name, locale, is_admin, email_verified, verification_token, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (email.lower(), password_hash, name, locale, 1 if is_admin else 0,
                 1 if email_verified else 0, verification_token, now),
            )
            uid = int(cur.lastrowid or 0)
        return get_user(uid)
    except sqlite3.IntegrityError:
        return None


def get_user(uid: int) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()
        return dict(row) if row else None


def verify_password(user: dict, password: str) -> bool:
    if not bcrypt or not user.get("password_hash"):
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), user["password_hash"].encode("utf-8"))
    except Exception:
        return False


def update_user(uid: int, **fields) -> bool:
    allowed = {"email", "name", "locale", "is_admin", "email_verified", "password_hash", "last_login_at"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed: continue
        if k == "is_admin" or k == "email_verified": v = 1 if v else 0
        sets.append(f"{k} = ?"); params.append(v)
    if not sets: return False
    sets.append("created_at = created_at")  # noop, prevents trailing-comma issues
    params.append(uid)
    with _dbmod.db() as conn:
        cur = conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


# ----- Sessions -----

def create_session(user_id: int, *, days: int = 30, ip: str | None = None, user_agent: str | None = None) -> str:
    token = secrets.token_hex(32)  # 64 hex chars
    now = int(time.time())
    expires = now + days * 86400
    with _dbmod.db() as conn:
        conn.execute(
            """INSERT INTO sessions (token, user_id, created_at, expires_at, ip, user_agent)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (token, user_id, now, expires, ip, user_agent),
        )
    return token


def get_session(token: str) -> dict | None:
    """Get session if not expired."""
    if not token: return None
    now = int(time.time())
    with _dbmod.db() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token = ? AND expires_at > ?",
            (token, now),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        return cur.rowcount > 0


def purge_expired_sessions() -> int:
    """Delete expired sessions. Run periodically."""
    now = int(time.time())
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        return cur.rowcount


# ----- Device tokens (mobile / API pairing) -----

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_device_token(name: str | None = None, user_id: int | None = None) -> str:
    """Create a paired-device bearer token. Returns the RAW token ONCE; only
    its hash is persisted (never store the raw token)."""
    raw = secrets.token_urlsafe(32)
    with _dbmod.db() as conn:
        conn.execute(
            "INSERT INTO device_tokens (token_hash, name, user_id, created_at) VALUES (?, ?, ?, ?)",
            (_hash_token(raw), name, user_id, int(time.time())),
        )
    return raw


def get_device_token(raw: str) -> dict | None:
    """Resolve a raw bearer token to its (non-revoked) device row, or None."""
    if not raw:
        return None
    with _dbmod.db() as conn:
        row = conn.execute(
            "SELECT * FROM device_tokens WHERE token_hash = ? AND revoked = 0",
            (_hash_token(raw),),
        ).fetchone()
    return dict(row) if row else None


def touch_device_token(raw: str) -> None:
    with _dbmod.db() as conn:
        conn.execute(
            "UPDATE device_tokens SET last_seen_at = ? WHERE token_hash = ?",
            (int(time.time()), _hash_token(raw)),
        )


def list_device_tokens() -> list[dict]:
    with _dbmod.db() as conn:
        rows = conn.execute(
            "SELECT id, name, user_id, created_at, last_seen_at, revoked "
            "FROM device_tokens ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_device_token(token_id: int) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("UPDATE device_tokens SET revoked = 1 WHERE id = ?", (token_id,))
        return cur.rowcount > 0


# ----- LLM usage metering (credits foundation) -----

def log_usage(*, provider: str, model: str | None = None,
              prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
    total = int(prompt_tokens or 0) + int(completion_tokens or 0)
    with _dbmod.db() as conn:
        conn.execute(
            "INSERT INTO usage_log (provider, model, prompt_tokens, completion_tokens, total_tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (provider, model, int(prompt_tokens or 0), int(completion_tokens or 0), total, int(time.time())),
        )


def usage_summary(days: int = 30) -> dict:
    since = int(time.time()) - days * 86400
    with _dbmod.db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(prompt_tokens),0) pt, COALESCE(SUM(completion_tokens),0) ct, "
            "COALESCE(SUM(total_tokens),0) tt FROM usage_log WHERE created_at >= ?", (since,)).fetchone()
        by = conn.execute(
            "SELECT provider, COALESCE(SUM(total_tokens),0) tt, COUNT(*) c FROM usage_log "
            "WHERE created_at >= ? GROUP BY provider ORDER BY tt DESC", (since,)).fetchall()
    return {
        "days": days, "calls": row["c"],
        "prompt_tokens": row["pt"], "completion_tokens": row["ct"], "total_tokens": row["tt"],
        "by_provider": [dict(b) for b in by],
    }
