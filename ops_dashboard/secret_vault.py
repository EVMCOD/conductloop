"""Every secret of the app, in one place, and never in plain text.

A person who downloads the app should not have to open a terminal to give
it a key, and the key should not be lying in a SQLite file that travels
with the data folder (or in a ``config.json`` that a sync service copies
to another machine).

Two backends, and only two:

* the **OS keychain** (macOS): the value goes to the login keychain and
  the app keeps nothing but a name.
* an **encrypted file** under the data dir, whose key comes from a
  passphrase the user types in the app. The passphrase is never stored:
  it is asked for when the vault is needed and forgotten when the app
  closes. Forget it and the secrets are gone — that is the price of a
  file that nobody else on the machine can read.

What it never does: write a secret in plain text. A machine with no
keychain and no passphrase has no safe place for a key, and the app says
so instead of pretending otherwise.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from . import secrets_store

log = logging.getLogger("conductloop.vault")

VAULT_FILE = ".vault"
VAULT_VERSION = 1
# 240k rounds of PBKDF2-HMAC-SHA256: about a quarter of a second on a modern
# Mac, which is the point — it makes guessing the passphrase expensive
# without making the user wait.
PBKDF2_ROUNDS = 240_000

_lock = threading.Lock()
# The passphrase of this session, in memory only. It is never written
# anywhere and never leaves this process.
_session_passphrase: str | None = None


class VaultLocked(RuntimeError):
    """The vault is encrypted and nobody has opened it this session."""


class VaultUnavailable(RuntimeError):
    """There is nowhere safe to put a secret."""


# ── where things live ──────────────────────────────────────────────────────

def vault_path() -> Path:
    """The encrypted vault file, when there is one."""
    return secrets_store.secrets_dir() / VAULT_FILE


def keychain_available() -> bool:
    """Is the OS keychain there to be used?"""
    return secrets_store.keychain_available()


def backend() -> str:
    """Where a secret would go right now.

    * ``keychain`` — the OS keychain, and it works.
    * ``encrypted`` — the encrypted file (it exists, or the user is about
      to create it with a passphrase).
    * ``locked`` — there is an encrypted vault and it is not open.
    """
    if keychain_available():
        return "keychain"
    if vault_path().exists():
        return "locked" if _session_passphrase is None else "encrypted"
    return "encrypted" if _session_passphrase is not None else "locked"


def is_open() -> bool:
    """Can the app read the secrets it has, right now?"""
    return backend() in ("keychain", "encrypted")


# ── the encrypted file ─────────────────────────────────────────────────────

def _derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt,
                                PBKDF2_ROUNDS, 32)


def _fernet(passphrase: str, salt: bytes):
    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(_derive(passphrase, salt))
    return Fernet(key)


def _read_raw() -> dict[str, Any] | None:
    path = vault_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("the vault file could not be read: %s", e)
        return None


def _decrypt(raw: dict[str, Any], passphrase: str | None) -> dict[str, str] | None:
    """The secrets inside the vault, or None when the passphrase is wrong."""
    if passphrase is None:
        raise VaultLocked("the vault is locked: unlock it to use the keys inside")
    try:
        salt = base64.b64decode(raw.get("salt") or "")
        token = str(raw.get("data") or "").encode("utf-8")
        payload = _fernet(passphrase, salt).decrypt(token)
    except VaultLocked:
        raise
    except Exception:
        # InvalidToken: the passphrase is not the one that wrote this file.
        return None
    try:
        data = json.loads(payload.decode("utf-8"))
    except ValueError:
        return None
    return {str(k): str(v) for k, v in (data or {}).items()}


def _write_raw(data: dict[str, str], passphrase: str) -> None:
    path = vault_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(16)
    token = _fernet(passphrase, salt).encrypt(json.dumps(data).encode("utf-8"))
    blob = {"v": VAULT_VERSION, "salt": base64.b64encode(salt).decode("ascii"),
            "data": token.decode("ascii")}
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(blob))
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


# ── the session passphrase ─────────────────────────────────────────────────

def unlock(passphrase: str) -> bool:
    """Open the vault with the user's passphrase.

    Returns True when the vault is open. A passphrase that does not open it
    returns False and the vault stays closed — and nothing is overwritten.
    """
    global _session_passphrase
    passphrase = passphrase or ""
    if not passphrase:
        return False
    with _lock:
        raw = _read_raw()
        if raw is not None:
            data = _decrypt(raw, passphrase)
            if data is None:
                return False
        _session_passphrase = passphrase
    return True


def lock() -> None:
    """Forget the passphrase. The vault closes until somebody opens it again."""
    global _session_passphrase
    with _lock:
        _session_passphrase = None


def passphrase_set() -> bool:
    """Is there a passphrase in memory for this session?"""
    return _session_passphrase is not None


# ── the vault ──────────────────────────────────────────────────────────────

def get(name: str) -> str | None:
    """The secret, or None when it is not there."""
    if not name:
        return None
    if keychain_available():
        value = secrets_store.keychain_get(name)
        if value is not None:
            return value
    with _lock:
        raw = _read_raw()
        if raw is None:
            return None
        data = _decrypt(raw, _session_passphrase)
        if data is None:
            return None
        return data.get(name)


def set(name: str, value: str) -> str:
    """Store a secret. Returns where it went.

    The OS keychain first, always. Only when there is no keychain — or when
    the keychain refuses the write — does the encrypted file take it, and
    then the passphrase is needed.
    """
    if not name:
        raise ValueError("a secret needs a name")
    if keychain_available() and secrets_store.keychain_set(name, value):
        return "keychain"
    with _lock:
        if _session_passphrase is None:
            raise VaultLocked(
                "there is no keychain on this machine: set a passphrase to keep "
                "the keys encrypted")
        data = _decrypt(_read_raw() or {}, _session_passphrase) or {}
        data[name] = value
        _write_raw(data, _session_passphrase)
    return "encrypted"


def delete(name: str) -> bool:
    """Remove a secret from wherever it is. True when it is gone."""
    removed = False
    if keychain_available() and secrets_store.keychain_delete(name):
        removed = True
    with _lock:
        raw = _read_raw()
        if raw is None:
            return removed
        try:
            data = _decrypt(raw, _session_passphrase)
        except VaultLocked:
            return removed
        if data and name in data:
            data.pop(name, None)
            _write_raw(data, _session_passphrase or "")
            removed = True
    return removed


def where(name: str) -> str:
    """Where a secret lives: ``keychain``, ``encrypted`` or ``nowhere``."""
    if not name:
        return "nowhere"
    if keychain_available() and secrets_store.keychain_get(name) is not None:
        return "keychain"
    raw = _read_raw()
    if raw is None:
        return "nowhere"
    try:
        data = _decrypt(raw, _session_passphrase)
    except VaultLocked:
        return "encrypted"
    if data and name in data:
        return "encrypted"
    return "nowhere"


def stored_names() -> list[str]:
    """The names of the secrets in the encrypted file (the keychain cannot
    be listed cheaply, and the app knows its own names anyway)."""
    raw = _read_raw()
    if raw is None:
        return []
    try:
        data = _decrypt(raw, _session_passphrase)
    except VaultLocked:
        return []
    return sorted(data or {})


# ── the names the app uses ─────────────────────────────────────────────────

def brain_key_name(provider: str) -> str:
    """The name of a brain provider's key.

    It is the name the keychain already uses, so the keys that are stored
    today keep working and nothing has to be re-entered.
    """
    return f"byok.{provider}.default"


def media_key_name(provider: str) -> str:
    """The name of a media provider's key."""
    return f"media.{provider}.api_key"
