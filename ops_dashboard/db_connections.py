"""External service connections (Cloudflare, GitHub, Notion, ...) with
Fernet-encrypted credentials at rest.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so
``db.<name>`` callers keep working. Uses the shared transaction context manager
via ``_dbmod.db()``. Fernet is imported the same guarded way as db.py.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

try:
    from cryptography.fernet import Fernet  # type: ignore
    _HAS_FERNET = True
except ImportError:  # pragma: no cover
    Fernet = None  # type: ignore
    _HAS_FERNET = False

from ops_dashboard import db as _dbmod

# ============================================================
# External connections (Cloudflare, GitHub, Notion, etc.)
# ============================================================

def _fernet_key() -> bytes:
    """The Fernet key that protects connection credentials at rest.

    Upgrade #10: stored in the OS keychain (macOS) or a 0600 store file, via
    secrets_store — no longer plaintext next to the encrypted data. A legacy
    ~/.ops-dashboard/.secret.key is migrated into the store on first use so
    existing installs keep decrypting their credentials.
    """
    from cryptography.fernet import Fernet

    from . import secrets_store

    def _factory() -> str:
        legacy = Path.home() / ".ops-dashboard" / ".secret.key"
        if legacy.exists():
            try:
                return legacy.read_bytes().decode("ascii")
            except Exception:
                pass
        return Fernet.generate_key().decode("ascii")

    return secrets_store.get_or_create_secret("fernet-key", _factory).encode("ascii")


def _encrypt_credentials(creds: dict) -> str:
    """Encrypt a credentials dict with Fernet. Required — no plaintext fallback.

    Audit 2026-06-23: removed plaintext 'plain:' fallback. Failing loud is
    better than silently leaking Cloudflare/GitHub secrets to disk.
    """
    from cryptography.fernet import Fernet
    f = Fernet(_fernet_key())
    plaintext = json.dumps(creds).encode("utf-8")
    return "enc:" + f.encrypt(plaintext).decode("ascii")


def _decrypt_credentials(stored: str | None) -> dict:
    """Reverse of _encrypt_credentials. Returns {} on failure.

    Audit 2026-06-23: removed 'legacy assume plain JSON' fallback. Only
    'enc:' prefix is accepted. 'plain:' legacy rows (from before encryption
    became required) are still readable for back-compat, but no NEW rows
    are written with 'plain:' (see _encrypt_credentials).
    """
    if not stored: return {}
    try:
        if stored.startswith("enc:"):
            if not Fernet: return {}
            plaintext = Fernet(_fernet_key()).decrypt(stored[4:].encode("ascii"))
            return json.loads(plaintext)
        if stored.startswith("plain:"):
            return json.loads(stored[5:])
        # Unknown format — refuse rather than risk leaking raw stored bytes
        # back to callers (audit H: 'legacy plain JSON' fallback was a leak vector)
        return {}
    except Exception:
        return {}


def add_connection(provider: str, name: str, credentials: dict, *,
                    config: dict | None = None) -> int:
    now = int(time.time())
    creds_blob = _encrypt_credentials(credentials)
    with _dbmod.db() as conn:
        cur = conn.execute(
            """INSERT INTO connections
               (provider, name, credentials, config, status, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'unknown', 1, ?, ?)""",
            (provider, name, creds_blob, json.dumps(config or {}), now, now),
        )
        return int(cur.lastrowid or 0)


def update_connection(conn_id: int, **fields) -> bool:
    allowed = {"name", "credentials", "config", "status", "enabled",
               "last_used_at", "last_check_at", "last_error"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed: continue
        if k == "credentials" and isinstance(v, dict):
            v = _encrypt_credentials(v)
        elif k == "config" and isinstance(v, dict):
            v = json.dumps(v)
        sets.append(f"{k} = ?"); params.append(v)
    if not sets: return False
    sets.append("updated_at = ?"); params.append(int(time.time())); params.append(conn_id)
    with _dbmod.db() as conn:
        cur = conn.execute(f"UPDATE connections SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def list_connections(provider: str | None = None) -> list[dict]:
    sql = "SELECT id, provider, name, credentials, config, status, enabled, last_used_at, last_check_at, last_error, created_at, updated_at FROM connections"
    params: list = []
    if provider:
        sql += " WHERE provider = ?"; params.append(provider)
    sql += " ORDER BY provider, name"
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_connection_by_id(conn_id: int) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute("SELECT * FROM connections WHERE id = ?", (conn_id,)).fetchone()
    if not row: return None
    d = dict(row)
    d["credentials"] = _decrypt_credentials(d.get("credentials"))
    try: d["config"] = json.loads(d.get("config") or "{}")
    except Exception: d["config"] = {}
    return d


def get_connection_by_name(provider: str, name: str) -> dict | None:
    with _dbmod.db() as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE provider = ? AND name = ?",
            (provider, name),
        ).fetchone()
    if not row: return None
    d = dict(row)
    d["credentials"] = _decrypt_credentials(d.get("credentials"))
    try: d["config"] = json.loads(d.get("config") or "{}")
    except Exception: d["config"] = {}
    return d


def delete_connection(conn_id: int) -> bool:
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM connections WHERE id = ?", (conn_id,))
        return cur.rowcount > 0


def record_connection_used(conn_id: int) -> None:
    with _dbmod.db() as conn:
        conn.execute(
            "UPDATE connections SET last_used_at = ?, updated_at = ? WHERE id = ?",
            (int(time.time()), int(time.time()), conn_id),
        )


def record_connection_check(conn_id: int, *, ok: bool, error: str | None = None) -> None:
    with _dbmod.db() as conn:
        conn.execute(
            """UPDATE connections
               SET status = ?, last_check_at = ?, last_error = ?, updated_at = ?
               WHERE id = ?""",
            ("ok" if ok else "fail", int(time.time()), error, int(time.time()), conn_id),
        )
