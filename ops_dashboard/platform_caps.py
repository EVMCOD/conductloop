"""What this platform can and cannot do, in one place.

ConductLoop is macOS-first. Some features depend on macOS-only pieces
(WKWebView and its network filter for the in-app browser, ``sandbox-exec`` for
running commands). This module is the single source of truth the server, the
sessions code and the UI ask, so an unavailable feature is switched off and
explained instead of failing half-way.

Rules:

* The answer depends only on ``sys.platform`` (read at call time, so tests can
  monkeypatch it) and on what is installed (the sandbox probe).
* Running commands is **never** allowed without an OS sandbox. There is no
  "run it anyway" flag here; Windows has no sandbox backend, so code sessions
  are refused there outright.
* macOS behaviour is the baseline: nothing here changes it.
"""
from __future__ import annotations

import os
import sys
from typing import Any

MACOS, LINUX, WINDOWS, OTHER = "macos", "linux", "windows", "other"

# Reason codes shared with the UI (browser.html, sessions.js).
PLATFORM_UNSUPPORTED = "platform_unsupported"
SANDBOX_MISSING = "sandbox_missing"
SANDBOX_DISABLED = "sandbox_disabled"

_NAMES = {MACOS: "macOS", LINUX: "Linux", WINDOWS: "Windows", OTHER: "this operating system"}


class PlatformUnsupported(RuntimeError):
    """The feature does not exist on this platform (not a temporary failure)."""

    code = PLATFORM_UNSUPPORTED

    def __init__(self, capability: str, message: str):
        super().__init__(message)
        self.capability = capability


def platform_id() -> str:
    p = sys.platform
    if p == "darwin":
        return MACOS
    if p.startswith("linux"):
        return LINUX
    if p.startswith("win") or p == "cygwin":
        return WINDOWS
    return OTHER


def platform_name() -> str:
    return _NAMES[platform_id()]


def browser_supported() -> bool:
    """The in-app browser exists only on macOS (the desktop app also checks
    the macOS version: 14+)."""
    return platform_id() == MACOS


def code_sessions_supported() -> bool:
    """Code sessions need POSIX file APIs (``dir_fd``/``O_NOFOLLOW``) and an
    OS sandbox. Windows has neither, so they are refused there."""
    return platform_id() != WINDOWS


def browser_message() -> str:
    if browser_supported():
        return ""
    return (f"The in-app browser is not available on {platform_name()}: it needs macOS 14 or "
            f"later (it relies on the macOS web view and its network filter).")


def code_message(reason: str | None) -> str:
    if reason is None:
        return ""
    if reason == PLATFORM_UNSUPPORTED:
        return (f"Running code (tests and linters) for agent sessions is turned off on "
                f"{platform_name()}: there is no OS sandbox for it yet, and ConductLoop never "
                f"runs commands without one.")
    if reason == SANDBOX_DISABLED:
        return ("Running commands is turned off because CONDUCTLOOP_SANDBOX=none is set. "
                "ConductLoop never runs commands without a sandbox.")
    tool = {MACOS: "sandbox-exec", LINUX: "bubblewrap (bwrap)"}.get(platform_id(), "an OS sandbox")
    extra = " Install the 'bubblewrap' package to turn it on." if platform_id() == LINUX else ""
    return (f"Running commands is turned off because {tool} is missing or does not work on this "
            f"machine. ConductLoop never runs commands without a sandbox.{extra}")


def require_code_sessions() -> None:
    """Raise ``PlatformUnsupported`` when code sessions cannot exist here."""
    if not code_sessions_supported():
        raise PlatformUnsupported("code_execution", code_message(PLATFORM_UNSUPPORTED))


def code_execution(backend: str | None) -> dict[str, Any]:
    """Whether commands can run; ``backend`` is ``sandbox.detect_backend()``."""
    if not code_sessions_supported():
        reason: str | None = PLATFORM_UNSUPPORTED
    elif backend is None:
        forced = os.environ.get("CONDUCTLOOP_SANDBOX", "").strip().lower() == "none"
        reason = SANDBOX_DISABLED if forced else SANDBOX_MISSING
    else:
        reason = None
    return {"available": reason is None, "reason": reason, "backend": backend,
            "message": code_message(reason)}


def capabilities(*, backend: str | None = ...) -> dict[str, Any]:  # type: ignore[assignment]
    """The payload of ``GET /api/platform``.

    ``backend`` defaults to a live ``sandbox.detect_backend()`` probe; tests
    pass it explicitly.
    """
    if backend is ...:
        from .sessions import sandbox
        backend = sandbox.detect_backend()
    from . import secrets_store
    keychain = bool(secrets_store.keychain_available())
    browser_ok = browser_supported()
    return {
        "ok": True,
        "platform": platform_id(),
        "platform_name": platform_name(),
        "browser": {"available": browser_ok,
                    "reason": None if browser_ok else PLATFORM_UNSUPPORTED,
                    "requires": "macOS 14+", "message": browser_message()},
        "code_execution": code_execution(backend),
        "keychain": {"available": keychain,
                     "message": "" if keychain else
                     "Keys are kept in an encrypted file on this machine; there is no system "
                     f"keychain integration on {platform_name()} yet."},
    }
