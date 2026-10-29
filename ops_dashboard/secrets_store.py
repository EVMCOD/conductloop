"""OS-keychain-backed secret storage with a file fallback.

On macOS, secrets are stored in the login Keychain via the ``security`` CLI
(values go over stdin, never argv — see ``_kc_set``).

On Linux (experimental), they go to the freedesktop Secret Service (GNOME
Keyring, KWallet's bridge, KeePassXC) through the ``keyring`` library, pinned to
its ``SecretService`` backend: no other ``keyring`` backend is ever accepted
(see ``_ss_load``). The backend is probed once, with a timeout, and the answer
is cached. Without a Secret Service (no D-Bus session, a headless box, a locked
keyring nobody answers) the keychain is simply *unavailable*: ``keychain_*``
say so, the BYOK keys go to the encrypted vault and connector tokens are
refused. The generic ``set_secret`` file fallback (generated keys such as the
DB Fernet key) is the pre-existing behaviour; on Linux it now logs a warning,
once per secret, so it is never silent.

Everywhere else — and if keychain access fails, or OPS_DISABLE_KEYCHAIN=1 — they
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
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("conductloop.secrets")

_SERVICE = "com.conductloop"


def _disabled() -> bool:
    return os.environ.get("OPS_DISABLE_KEYCHAIN") == "1"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _keychain_enabled() -> bool:
    if _disabled():
        return False
    if sys.platform == "darwin":
        return shutil.which("security") is not None
    if _is_linux():
        return _ss_backend() is not None
    return False


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
    if _is_linux():
        return _ss_get(name)
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
    if _is_linux():
        _ss_delete(name)
        return
    try:
        subprocess.run([_security_bin(), "delete-generic-password", "-a", name, "-s", _SERVICE],
                       capture_output=True, text=True, timeout=5)
    except Exception as e:
        log.debug("keychain delete failed for %s: %s", name, type(e).__name__)


def _kc_write(name: str, value: str) -> bool:
    if _is_linux():
        return _ss_write(name, value)
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


# ── Linux: freedesktop Secret Service through ``keyring`` ────────────────────
# The Rust shell never reads secrets, and the PyInstaller sidecar cannot assume
# ``secret-tool`` exists, so the Python side talks to the Secret Service itself.
# Same names as macOS: service ``com.conductloop``, account = the secret name,
# so ``secret-tool lookup service com.conductloop username <name>`` finds them.
#
# The agents' command sandbox hides the D-Bus session bus, and this module is
# never imported by a sandboxed process: only the app's own server talks to the
# Secret Service. (``sessions/sandbox.py`` builds a fresh environment, too.)
#
# Every call runs in a worker thread with a timeout: a locked collection can
# block on an unlock prompt nobody sees, and a half-dead bus can hang.

_SS_MODULE = "keyring.backends.SecretService"
_SS_PROBE_TIMEOUT = 8.0     # seconds for the whole probe (connect + set/get/delete)
_SS_OP_TIMEOUT = 15.0       # seconds for one read/write/delete
_SS_RETRY_AFTER = 60.0      # a failed probe is not repeated for this long

_ss_lock = threading.Lock()
_ss_state: dict = {"kr": None, "reason": None, "at": 0.0, "warned": False}
_fallback_warned: set[str] = set()


class _SSUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _ss_load():
    """The ``keyring`` SecretService backend, and nothing else.

    ``keyring`` can resolve to a ``fail``, chainer or null backend, and
    ``keyrings.alt`` ships plaintext/obfuscated file backends. We never ask
    ``keyring`` to choose: we build the one class we allow and check it.
    """
    try:
        from keyring.backends import SecretService as mod
    except Exception:
        raise _SSUnavailable("keyring_missing") from None
    try:
        kr = mod.Keyring()
    except Exception:
        raise _SSUnavailable("backend_init_failed") from None
    cls = type(kr)
    if f"{cls.__module__}.{cls.__qualname__}" != f"{_SS_MODULE}.Keyring":
        raise _SSUnavailable("backend_not_allowed")
    return kr


def _ss_run(fn, timeout: float):
    """Run ``fn()`` in a daemon thread; ``TimeoutError`` if it does not return."""
    box: dict = {}

    def work() -> None:
        try:
            box["v"] = fn()
        except BaseException as e:  # handed to the caller
            box["e"] = e

    t = threading.Thread(target=work, name="secret-service", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError("secret service did not answer")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _ss_probe(kr) -> None:
    """Prove the Secret Service works: a D-Bus connection, then a write, a read
    and a delete of a throwaway item. Raises ``_SSUnavailable``."""
    account = f"__conductloop-probe-{uuid.uuid4().hex}"
    probe_value = uuid.uuid4().hex

    def go() -> None:
        try:
            _ = kr.priority  # raises RuntimeError without a usable Secret Service
        except Exception:
            raise _SSUnavailable("no_secret_service") from None
        try:
            kr.set_password(_SERVICE, account, probe_value)
            ok = kr.get_password(_SERVICE, account) == probe_value
        except Exception:
            raise _SSUnavailable("probe_failed") from None
        finally:
            try:
                kr.delete_password(_SERVICE, account)
            except Exception:
                pass
        if not ok:
            raise _SSUnavailable("probe_failed")

    try:
        _ss_run(go, _SS_PROBE_TIMEOUT)
    except TimeoutError:
        raise _SSUnavailable("timeout") from None


def _ss_backend():
    """The probed backend, or ``None`` (cached; a failure is retried later)."""
    if _disabled() or not _is_linux():
        return None
    with _ss_lock:
        if _ss_state["kr"] is not None:
            return _ss_state["kr"]
        if _ss_state["reason"] and time.monotonic() - _ss_state["at"] < _SS_RETRY_AFTER:
            return None
        try:
            kr = _ss_load()
            _ss_probe(kr)
        except _SSUnavailable as e:
            _ss_state.update(kr=None, reason=e.reason, at=time.monotonic())
            if not _ss_state["warned"]:
                _ss_state["warned"] = True
                log.warning("Secret Service keychain unavailable (%s); secrets that need a keychain "
                            "will not be stored in it (see docs/PORTABILITY.md)", e.reason)
            return None
        _ss_state.update(kr=kr, reason=None, at=0.0)
        return kr


def _ss_reset() -> None:
    """Forget the cached probe (tests; also what a retry after a fix would use)."""
    with _ss_lock:
        _ss_state.update(kr=None, reason=None, at=0.0, warned=False)
    _fallback_warned.clear()


def _ss_name_ok(name: str) -> bool:
    return bool(name) and not any(c in name for c in "\r\n\0")


def _ss_get(name: str) -> str | None:
    kr = _ss_backend()
    if kr is None or not _ss_name_ok(name):
        return None
    try:
        return _ss_run(lambda: kr.get_password(_SERVICE, name), _SS_OP_TIMEOUT)
    except Exception as e:  # never log output: it would be the secret
        log.debug("secret service read failed for %s: %s", name, type(e).__name__)
        return None


def _ss_write(name: str, value: str) -> bool:
    kr = _ss_backend()
    if kr is None or not _ss_name_ok(name) or not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
        _ss_run(lambda: kr.set_password(_SERVICE, name, value), _SS_OP_TIMEOUT)
    except Exception as e:
        log.debug("secret service write failed for %s: %s", name, type(e).__name__)
        return False
    # Like the macOS path: only report success when the value round-trips.
    return _ss_get(name) == value


def _ss_delete(name: str) -> bool:
    """Remove ``name``. True when the item is gone afterwards (also when it was
    never there); False when the Secret Service could not confirm that."""
    kr = _ss_backend()
    if kr is None or not _ss_name_ok(name):
        return False
    try:
        try:
            _ss_run(lambda: kr.delete_password(_SERVICE, name), _SS_OP_TIMEOUT)
        except TimeoutError:
            raise
        except Exception as e:
            # keyring raises PasswordDeleteError when there is nothing to delete.
            log.debug("secret service delete for %s: %s", name, type(e).__name__)
        return _ss_run(lambda: kr.get_password(_SERVICE, name), _SS_OP_TIMEOUT) is None
    except Exception as e:
        log.debug("secret service delete failed for %s: %s", name, type(e).__name__)
        return False


def _warn_file_fallback(name: str) -> None:
    """On Linux, say (once per secret) that a secret went to a 0600 file because
    no Secret Service took it. Never prints the value."""
    if not _is_linux() or _disabled() or name in _fallback_warned:
        return
    _fallback_warned.add(name)
    log.warning("secret %r is kept in a 0600 file under %s: no Secret Service keychain took it. "
                "Install and unlock GNOME Keyring, KWallet or KeePassXC to use the system keychain.",
                name, _secrets_dir())


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
    _warn_file_fallback(name)
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


def keychain_status() -> dict:
    """Which keychain backs this machine and, if none, why not.

    ``{"backend": "macos-security" | "secret-service" | None, "available": bool,
    "reason": None | "disabled" | "unsupported_platform" | "no_security_tool" |
    "keyring_missing" | "backend_init_failed" | "backend_not_allowed" |
    "no_secret_service" | "probe_failed" | "timeout"}``. Never carries a value.
    """
    if _disabled():
        return {"backend": None, "available": False, "reason": "disabled"}
    if sys.platform == "darwin":
        ok = shutil.which("security") is not None
        return {"backend": "macos-security" if ok else None, "available": ok,
                "reason": None if ok else "no_security_tool"}
    if _is_linux():
        ok = _ss_backend() is not None
        return {"backend": "secret-service" if ok else None, "available": ok,
                "reason": None if ok else _ss_state["reason"]}
    return {"backend": None, "available": False, "reason": "unsupported_platform"}


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
    if _is_linux():
        return _ss_delete(name)
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
