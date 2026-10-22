"""Per-launch app token for browser requests over loopback (browser design PR 2).

Threat: the in-app browser (design doc 2026-10-21, §1.3) shows untrusted pages
on the same machine. Origin checks (#24) and Host checks (#26) do not stop a
tab from *navigating* to ``http://localhost:7777/``: that load has a valid Host,
no Origin, and once loaded its origin is the app's own. Neither do they protect
read-only GETs from side effects that are not on a list.

Fix: the desktop wrapper (Tauri) generates two random values per launch and
passes them to the sidecar via the environment:

* ``CONDUCTLOOP_APP_TOKEN`` — the secret. It lives in an ``HttpOnly`` cookie
  (``cl_app``) of the UI webview only. Browser tabs use a separate data store,
  so they never have it and cannot read or set it.
* ``CONDUCTLOOP_APP_BOOT`` — a **single-use** nonce. The wrapper first loads
  ``/__cl/boot?n=<nonce>``; the server answers with ``Set-Cookie: cl_app=…`` and
  a redirect to ``/``. After that the nonce is burnt.

Enforcement (only when ``CONDUCTLOOP_APP_TOKEN`` is set, i.e. inside the app):
a loopback request that looks like it comes from a browser engine — it has an
``Origin`` header or any ``Sec-Fetch-*`` header, which pages cannot remove —
must carry the cookie, for every method including GET. Otherwise: 403
``app_token_required``. Non-browser local clients (CLI, curl, scripts: no
Origin, no Sec-Fetch-*) keep the historic loopback trust. Remote requests
(LAN/Tailscale/tunnels) are untouched: they already need a session or bearer.

``make serve`` / ``python -m ops_dashboard serve`` without the variable: no
change at all.
"""
from __future__ import annotations

import hmac
import os
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

TOKEN_ENV = "CONDUCTLOOP_APP_TOKEN"
BOOT_ENV = "CONDUCTLOOP_APP_BOOT"
COOKIE = "cl_app"
BOOT_PATH = "/__cl/boot"
MIN_LEN = 32

# Read-only, secret-free endpoints that other origins legitimately call
# (marketing site pricing / health checks). Kept in sync with
# origin_guard.PUBLIC_CORS_PATHS (#24).
EXEMPT_PATHS = frozenset({"/api/health", "/health", "/api/pricing-tier"})

_boot_lock = threading.Lock()
_boot_used = False


def _env(name: str) -> str:
    v = (os.environ.get(name) or "").strip()
    return v if len(v) >= MIN_LEN else ""


def enabled() -> bool:
    return bool(_env(TOKEN_ENV))


def _reset_for_tests() -> None:
    global _boot_used
    with _boot_lock:
        _boot_used = False


def looks_like_browser(headers: Any) -> bool:
    if headers.get("Origin") is not None:
        return True
    return any(k.lower().startswith("sec-fetch-") for k in headers.keys())


def _cookie_value(headers: Any) -> str:
    raw = headers.get("Cookie", "") or ""
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value.strip()
    return ""


def has_valid_cookie(headers: Any) -> bool:
    token = _env(TOKEN_ENV)
    got = _cookie_value(headers)
    return bool(token and got) and hmac.compare_digest(got.encode(), token.encode())


def _consume_boot(nonce: str) -> bool:
    global _boot_used
    expected = _env(BOOT_ENV)
    if not expected or not nonce:
        return False
    with _boot_lock:
        if _boot_used:
            return False
        if not hmac.compare_digest(nonce.encode(), expected.encode()):
            return False
        _boot_used = True
        return True


def _deny(handler: Any, status: int, code: str, message: str) -> bool:
    import json

    body = json.dumps({"ok": False, "error": code, "code": code, "message": message}).encode()
    handler.close_connection = True
    handler._send(status, body, "application/json", {"Connection": "close"})
    return False


def check(handler: Any) -> bool:
    """Run before any do_* method. True = continue; False = response sent."""
    path = urlparse(getattr(handler, "path", "/") or "/").path
    if not enabled():
        if path == BOOT_PATH:
            # App wrapper talking to a server started without the token (e.g. a
            # `make serve` already holding :7777): just land on the UI.
            handler._send(302, b"", "text/html", {"Location": "/"})
            return False
        return True
    if not handler._is_local():
        return True
    if path == BOOT_PATH:
        q = parse_qs(urlparse(handler.path).query)
        nonce = (q.get("n") or [""])[0]
        if handler.command != "GET" or not _consume_boot(nonce):
            return _deny(handler, 403, "app_boot_invalid", "boot nonce missing, invalid or already used")
        # Session cookie (no Max-Age): dies with the app. Lax, not Strict: the
        # first load comes from the tauri:// loading page (another scheme =
        # cross-site under schemeful same-site) and must carry the cookie.
        # SameSite is not the boundary here anyway: browser tabs have a
        # separate cookie store.
        cookie = f"{COOKIE}={_env(TOKEN_ENV)}; Path=/; HttpOnly; SameSite=Lax"
        handler._send(302, b"", "text/html", {"Location": "/", "Set-Cookie": cookie})
        return False
    if path in EXEMPT_PATHS:
        return True
    if not looks_like_browser(handler.headers):
        return True
    if has_valid_cookie(handler.headers):
        return True
    return _deny(handler, 403, "app_token_required",
                 "this local API only accepts browser requests from the ConductLoop window")
