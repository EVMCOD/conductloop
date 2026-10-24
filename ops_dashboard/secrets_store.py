"""OS-keychain-backed secret storage with a file fallback.

On macOS, secrets are stored in the login Keychain via the ``security`` CLI
(values go over stdin, never argv — see ``_kc_set``).
Everywhere else — and if Keychain access fails, or OPS_DISABLE_KEYCHAIN=1 — they
fall back to 0600 files under the data dir (the pre-existing behavior). Use for
the Fernet DB-encryption key, license key, and other at-rest secrets.

Env: OPS_DISABLE_KEYCHAIN=1 (force file fallback), OPS_SECRETS_DIR (file dir).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("conductloop.secrets")

_SERVICE = "com.conductloop"


def _keychain_enabled() -> bool:
    if os.environ.get("OPS_DISABLE_KEYCHAIN") == "1":
        return False
    return sys.platform == "darwin" and shutil.which("security") is not None


def _secrets_dir() -> Path:
    env = os.environ.get("OPS_SECRETS_DIR")
    if env:
        return Path(env)
    from . import db  # lazy: reuse the resolved data dir, no import cycle
    return Path(db._DB_DIR)


def _file(name: str) -> Path:
    return _secrets_dir() / f".{name}.secret"


# The ``security`` CLI must never receive a secret VALUE as an argument: argv
# is visible to every local user via ``ps``. Reads only pass the item name;
# writes run ``security -i`` and send the command on stdin, with the value
# hex-encoded (``-X``) so the interactive parser never sees quotes, spaces or
# backslashes from the secret. Same binary as before, so existing items keep
# their Keychain ACLs (no new access prompts).
_SECURITY = "/usr/bin/security"
# ``security -i`` reads at most 4096 bytes per line (MAX_LINE_LEN); a longer
# line would be split/truncated, so refuse instead of storing a partial value.
_MAX_LINE = 4000


def _security_bin() -> str:
    return _SECURITY if os.path.exists(_SECURITY) else (shutil.which("security") or _SECURITY)


def _quote_arg(text: str) -> str:
    """Quote ``text`` for ``security -i``'s line parser (split_line in Apple's
    SecurityTool): inside double quotes a backslash escapes the next char."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _kc_get(name: str) -> str | None:
    try:
        out = subprocess.run(
            [_security_bin(), "find-generic-password", "-a", name, "-s", _SERVICE, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.rstrip("\n")
    except Exception as e:  # never log output: it would be the secret
        log.debug("keychain read failed for %s: %s", name, type(e).__name__)
    return None


def _kc_delete(name: str) -> None:
    """Remove ``name``'s item (argv carries only the name). Used when a write is
    refused so a stale or undecodable keychain value can't shadow the file."""
    try:
        subprocess.run([_security_bin(), "delete-generic-password", "-a", name, "-s", _SERVICE],
                       capture_output=True, text=True, timeout=5)
    except Exception as e:
        log.debug("keychain delete failed for %s: %s", name, type(e).__name__)


def _kc_write(name: str, value: str) -> bool:
    try:
        data = value.encode("utf-8")
    except Exception:
        return False
    # -X <hex>: only [0-9a-f] on the line; "-w ''" for the (non-secret) empty value.
    pw = f"-X {data.hex()}" if data else '-w ""'
    line = f"add-generic-password -U -a {_quote_arg(name)} -s {_quote_arg(_SERVICE)} {pw}\n"
    if len(line.encode("utf-8")) > _MAX_LINE:
        log.debug("keychain write skipped for %s: value too long for security -i", name)
        return False
    try:
        r = subprocess.run(
            [_security_bin(), "-i"], input=line,
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        log.debug("keychain write failed for %s: %s", name, type(e).__name__)
        return False
    if r.returncode != 0:
        log.debug("keychain write failed for %s (exit %s)", name, r.returncode)
        return False
    # `security -i` may exit 0 even if the command failed, and `-w` prints
    # non-printable data as hex: only report success when the value round-trips.
    return _kc_get(name) == value


def _kc_set(name: str, value: str) -> bool:
    """Write via ``security -i`` + stdin; True only if a read-back matches.
    On False, any existing item for ``name`` is removed (the caller's fallback
    then holds the current value)."""
    if not name or any(c in name for c in "\r\n\0"):
        return False
    if _kc_write(name, value):
        return True
    _kc_delete(name)
    return False


def get_secret(name: str) -> str | None:
    if _keychain_enabled():
        v = _kc_get(name)
        if v is not None:
            return v
    p = _file(name)
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").rstrip("\n")
        except Exception:
            return None
    return None


def set_secret(name: str, value: str) -> None:
    kc = _keychain_enabled() and bool(name) and not any(c in name for c in "\r\n\0")
    if kc and _kc_write(name, value):
        return
    p = _file(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value)
    os.replace(tmp, p)          # raises before touching the keychain if the disk fails
    if kc:
        # Only now drop the stale keychain item, so it can't shadow the file.
        # Deleting it first would lose the only copy if the file write failed.
        _kc_delete(name)


# ── Keychain-only API (no file fallback) ─────────────────────────────────────
# For secrets that must NOT silently land in a plaintext file when the OS
# keychain is unavailable (e.g. BYOK provider keys): callers check
# keychain_available() and keep their own behaviour otherwise.

def keychain_available() -> bool:
    return _keychain_enabled()


def keychain_get(name: str) -> str | None:
    return _kc_get(name) if _keychain_enabled() else None


def keychain_set(name: str, value: str) -> bool:
    """Store in the OS keychain only. Returns False (never falls back to a file)
    when the keychain is unavailable or the write fails."""
    return _keychain_enabled() and _kc_set(name, value)


def keychain_delete(name: str) -> bool:
    """Remove an item from the OS keychain only.

    Returns True when the item is gone afterwards — including when it was
    never there. Never touches the file fallback: a caller that wants both
    asks for both.
    """
    if not _keychain_enabled():
        return False
    _kc_delete(name)
    return _kc_get(name) is None


def secrets_dir() -> Path:
    """Where the file fallback keeps the secrets."""
    return _secrets_dir()


def get_or_create_secret(name: str, factory) -> str:
    """Return the stored secret, or create+store one via ``factory()`` (a
    no-arg callable returning str)."""
    v = get_secret(name)
    if v is not None:
        return v
    v = factory()
    set_secret(name, v)
    return v
