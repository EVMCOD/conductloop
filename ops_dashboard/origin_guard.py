"""Origin allowlist for CORS and cross-site write protection of the local API.

Why: the server trusts loopback requests without a session or CSRF token (the
desktop owner at the machine). Before this module it also reflected *any*
``Origin`` with ``Access-Control-Allow-Credentials: true``, so any web page open
in the user's browser could drive the local API (classic localhost CSRF), and
read the answers too.

Policy
------
* **CORS** is only granted (with credentials) to :func:`allowed_origins`: the
  Tauri desktop app origins and the local dashboard itself on its bound port
  (``http://localhost:<port>`` / ``http://127.0.0.1:<port>``, plus the default
  port 7777). The public read-only GETs in ``PUBLIC_CORS_PATHS`` (pricing tier,
  health) keep ``Access-Control-Allow-Origin: *`` **without** credentials, for the
  marketing site. Any other origin gets no CORS headers at all.
* **Writes** (POST/PUT/PATCH/DELETE) and **every** ``/api/*`` GET except
  ``PUBLIC_CORS_PATHS`` are rejected with a stable 403 JSON
  (``forbidden_origin``) when ``Sec-Fetch-Site: cross-site``, ``Origin: null`` or
  an ``Origin`` outside the allowlist is sent — **including from loopback**.
  GETs are covered wholesale because the legacy ``_route`` chain has GET
  handlers that call the LLM, write files or sync with third parties
  (``/api/brain/briefing``, ``/api/digest``, ``/api/github/sync``…), and a
  hand-kept list kept missing some. A foreign page could never read those
  responses anyway (no CORS), so refusing them costs no legitimate use.
* Requests **without** ``Origin`` and without ``Sec-Fetch-Site: cross-site``
  (curl, the CLI, server-to-server webhooks such as Stripe/Telegram) are still
  accepted: browsers always send ``Origin`` on cross-origin POST/PUT/DELETE and
  on any same-origin non-GET, so a missing Origin means a non-browser client.
  Those keep the existing gate (loopback trusted; remote needs auth + CSRF).
* Non-loopback requests (a paired phone on LAN/Tailscale using the web UI) may
  also use the same origin as the ``Host`` they reached. That traffic is still
  behind the existing session/Bearer auth and CSRF double-submit. The Host
  fallback is never used for loopback, so DNS rebinding to 127.0.0.1 with a
  foreign Host does not pass.

Extra origins can be allowed with ``OPS_ALLOWED_ORIGINS`` (comma separated,
exact ``scheme://host[:port]``).
"""
from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_PORT = 7777

# Tauri v2 asset origins: macOS/Linux use the custom scheme, Windows serves the
# bundled frontend from http(s)://tauri.localhost. The ConductLoop window itself
# navigates to http://localhost:7777 (desktop/src-tauri/src/lib.rs BACKEND_URL),
# so in practice its API calls are same-origin with the dashboard.
TAURI_ORIGINS = ("tauri://localhost", "http://tauri.localhost", "https://tauri.localhost")

PUBLIC_CORS_PATHS = frozenset({"/api/pricing-tier", "/api/health", "/health"})

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# GET routes that change state, spend LLM tokens or call out (the legacy
# _route chain dispatches them for any method). Audited 2026-10-21.
SIDE_EFFECT_GETS = frozenset({
    "/api/auth/login", "/api/auth/signup", "/api/auth/logout",
    "/api/billing/checkout-lifetime", "/api/billing/checkout-subscription",
    "/api/billing/cancel-subscription",
    "/api/boardroom/recap/refresh",
    "/api/brain/ask", "/api/brain/ask-tools", "/api/brain/stream", "/api/brain/briefing",
    "/api/digest", "/api/digest/ai-summary", "/api/github/sync",
    "/api/docs/create",
    "/api/revenue/seed-mock", "/api/sentry/seed-mock", "/api/cost/seed-mock", "/api/ga/seed-mock",
    "/api/telegram/set_webhook", "/api/telegram/clear_webhook", "/api/telegram/chats-bind",
    "/api/voice/transcribe",
    "/api/webhooks/test",
})


def _port(handler: Any) -> int:
    try:
        return int(handler.server.server_address[1])
    except Exception:
        return DEFAULT_PORT


def allowed_origins(port: int | None = None) -> frozenset[str]:
    ports = {DEFAULT_PORT, port or DEFAULT_PORT}
    out = set(TAURI_ORIGINS)
    for p in ports:
        out.update({f"http://localhost:{p}", f"http://127.0.0.1:{p}", f"http://[::1]:{p}"})
    for extra in (os.environ.get("OPS_ALLOWED_ORIGINS") or "").split(","):
        extra = extra.strip().rstrip("/")
        if extra:
            out.add(extra)
    return frozenset(out)


def _is_loopback(handler: Any) -> bool:
    try:
        return bool(handler._is_local())
    except Exception:
        return False


def _same_host_origin(handler: Any, origin: str) -> bool:
    host = (handler.headers.get("Host") or "").strip()
    return bool(host) and origin in (f"http://{host}", f"https://{host}")


def origin_allowed(handler: Any, origin: str) -> bool:
    if origin in allowed_origins(_port(handler)):
        return True
    # Remote (authenticated) browser UI served from this same server.
    return not _is_loopback(handler) and _same_host_origin(handler, origin)


def needs_check(method: str, path: str) -> bool:
    if method in WRITE_METHODS:
        return True
    if method not in ("GET", "HEAD"):
        return False
    return path in SIDE_EFFECT_GETS or (path.startswith("/api/") and path not in PUBLIC_CORS_PATHS)


def rejection(handler: Any) -> str | None:
    """Reason code if this request must be refused, else None."""
    origin = handler.headers.get("Origin")
    origin = origin.strip().rstrip("/") if origin is not None else None
    if origin and origin.lower() != "null" and origin in allowed_origins(_port(handler)):
        return None          # explicitly allowlisted (e.g. a Tauri asset page is cross-site by nature)
    if (handler.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
        return "cross_site"
    if origin is None:
        return None          # non-browser client (curl/CLI/webhook): existing gate applies
    if origin.lower() == "null" or origin == "":
        return "origin_null"
    if not origin_allowed(handler, origin):
        return "origin_not_allowed"
    return None


MESSAGES = {
    "cross_site": "cross-site requests cannot modify the local API",
    "origin_null": "requests with Origin: null cannot modify the local API",
    "origin_not_allowed": "this origin is not allowed to modify the local API",
}


def guard(handler: Any, method: str, path: str) -> bool:
    """Send a 403 and return False if the write must be refused."""
    if not needs_check(method, path):
        return True
    reason = rejection(handler)
    if reason is None:
        return True
    body = json.dumps({"ok": False, "error": "forbidden_origin", "code": "forbidden_origin",
                       "reason": reason, "message": MESSAGES[reason]}).encode()
    handler._send(403, body, "application/json")
    return False


def cors_headers(handler: Any, path: str) -> dict[str, str]:
    """CORS response headers for this request (empty for foreign origins)."""
    origin = (handler.headers.get("Origin") or "").strip().rstrip("/")
    if origin and origin.lower() != "null" and origin_allowed(handler, origin):
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                "Access-Control-Allow-Credentials": "true"}
    if path in PUBLIC_CORS_PATHS:
        return {"Access-Control-Allow-Origin": "*", "Vary": "Origin"}
    return {"Vary": "Origin"} if origin else {}
