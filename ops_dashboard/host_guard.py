"""Host-header check for loopback requests (DNS-rebinding protection).

Why: the server trusts requests that arrive over loopback without any session
(the desktop owner at the machine). A DNS-rebinding page (``evil.example``
re-resolved to 127.0.0.1) makes the user's browser talk to the local server
*same-origin*: no CORS, no Origin on GETs, and every response readable. The one
thing the attacker cannot forge from a browser is the ``Host`` header — it is
always the attacker's name. So loopback requests must name this server.

Policy
------
* Applies **only** to requests that ``_is_local()`` treats as loopback (client
  127.0.0.1/::1 and no ``X-Forwarded-For`` / ``X-Relay``). Remote requests
  (paired phone over LAN/Tailscale) are unchanged: they already need auth.
* Allowed ``Host`` values: ``localhost``, ``127.0.0.1`` and ``[::1]`` with the
  real bound port and the default 7777 (port-less only when the port is 80),
  plus the host[:port] of every entry in ``OPS_ALLOWED_ORIGINS`` (comma
  separated origins) and of ``OPS_DASHBOARD_BASE_URL``. Comparison is
  case-insensitive; a trailing dot (``localhost.``) is not accepted.
* The Tauri window loads ``http://localhost:7777`` (desktop/src-tauri/src/lib.rs),
  so it sends ``Host: localhost:7777``. ``tauri.localhost`` is Tauri's internal
  asset protocol, answered inside the app, so it never reaches this server and
  is not allowed.
* No ``Host`` at all (HTTP/1.0 tools): allowed. Browsers always send Host, so a
  missing Host is never a rebinding browser.
* Anything else from loopback: **421 Misdirected Request** with a stable JSON
  ``{"ok": false, "error": "invalid_host", "code": "invalid_host", ...}``.
  421 (RFC 9110 §15.5.20) says exactly "this server is not authoritative for
  that authority", which is the situation; 403 would suggest a permissions
  problem that logging in could fix.
* Tunnels (cloudflared, ngrok, Tailscale Serve/Funnel) that deliver
  Telegram webhooks to localhost all add ``X-Forwarded-For``, so those requests
  are already "remote" for ``_is_local()`` and this check does not touch them.
  A tunnel configured to strip X-Forwarded-For *and* keep the public Host would
  now get 421 — before this check it was treated as the trusted local owner.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlsplit

DEFAULT_PORT = 7777
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "[::1]")


def _netloc(url: str) -> str | None:
    url = url.strip()
    if not url:
        return None
    try:
        parts = urlsplit(url if "//" in url else "//" + url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    default = {"http": 80, "https": 443}.get(parts.scheme or "http")
    return host if port is None or port == default else f"{host}:{port}"


def allowed_hosts(port: int | None) -> frozenset[str]:
    out: set[str] = set()
    for p in {DEFAULT_PORT, port or DEFAULT_PORT}:
        for name in LOOPBACK_NAMES:
            out.add(f"{name}:{p}")
            if p == 80:
                out.add(name)
    extra = (os.environ.get("OPS_ALLOWED_ORIGINS") or "").split(",")
    extra.append(os.environ.get("OPS_DASHBOARD_BASE_URL") or "")
    for item in extra:
        n = _netloc(item)
        if n:
            out.add(n)
            if not n.rsplit("]", 1)[-1].count(":"):      # no explicit port: default one
                scheme = urlsplit(item.strip()).scheme or "http"
                out.add(f"{n}:{443 if scheme == 'https' else 80}")
    return frozenset(out)


def _port(handler: Any) -> int:
    try:
        return int(handler.server.server_address[1])
    except Exception:
        return DEFAULT_PORT


def host_ok(handler: Any) -> bool:
    try:
        local = bool(handler._is_local())
    except Exception:
        local = False
    if not local:
        return True
    host = handler.headers.get("Host")
    if host is None:
        return True
    return host.strip().lower() in allowed_hosts(_port(handler))


def check(handler: Any) -> bool:
    """True if the request may proceed; otherwise answer 421 and return False."""
    if host_ok(handler):
        return True
    body = json.dumps({
        "ok": False, "error": "invalid_host", "code": "invalid_host",
        "message": "this server only answers loopback requests addressed to localhost, "
                   "127.0.0.1 or [::1] (DNS-rebinding protection)",
    }).encode()
    handler.close_connection = True
    handler._send(421, body, "application/json")
    return False
