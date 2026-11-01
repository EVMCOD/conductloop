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
from pathlib import Path
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


# Where the token is kept between launches. Tests point it elsewhere.
_DATA_DIR: Path | None = None


def _token_path() -> Path:
    base = _DATA_DIR
    if base is None:
        from .app_paths import data_dir
        base = data_dir()
    return base / "app-token"


def resolve_token() -> str:
    """The session token, persisted so a restart does not break the app.

    The desktop app hands a token to the server in the environment, once per
    launch. The browser keeps that token as a cookie, and every request from
    the window has to carry it. If the token changed on every launch, a
    server restart — an update, a crash, the scheduler — would leave the open
    window holding a cookie that no longer matches: every request would
    answer 403 and the app would look broken.

    So the token is kept next to the data, and the copy on disk is the one
    that is used: it is the one the window that is already open has.

    With no token in the environment there is nothing to persist, and the
    guard stays off — that is the development server, which must keep
    working without a cookie.
    """
    path = _token_path()
    try:
        stored = path.read_text(encoding="utf-8").strip()
        if len(stored) >= MIN_LEN:
            return stored
    except OSError:
        pass
    token = _env(TOKEN_ENV)
    if not token:
        # No token in the environment: the guard is off (the development
        # server), and there is nothing worth persisting.
        return ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def enabled() -> bool:
    # The environment decides whether the guard is on at all. The file on
    # disk only chooses WHICH token is used. That distinction matters: a
    # file left behind by an earlier run must not silently turn the guard
    # on for a development server that never asked for it.
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
    token = resolve_token()
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
    # The page itself: hand the window a cookie that works instead of an
    # error. The window keeps the token in a cookie, and if that cookie
    # stopped matching (the server was restarted with a different token),
    # a reload used to answer 403: the app would not load at all, and the
    # only way out was to quit and reopen it.
    #
    # Only the page does this. The API never hands a token out, so a page
    # on the open web cannot ask for one and use it.
    if path == "/" or path.endswith(".html"):
        token = resolve_token()
        if token:
            handler._pending_headers = {
                "Set-Cookie": f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"}
            return True
    return _deny(handler, 403, "app_token_required",
                 "this local API only accepts browser requests from the ConductLoop window")
