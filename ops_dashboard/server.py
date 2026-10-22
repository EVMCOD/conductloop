"""Local web dashboard.

Serves a single-page dashboard at http://localhost:7777/ backed by JSON.
Run with:  python -m ops_dashboard serve [--port 7777]
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import typer
from rich.console import Console

from . import db, licensing, projects
from . import provider_errors as _pe
from .indexers import live_urls
from .logging_setup import get_logger
from .routing import router as _router

_log = get_logger(__name__)


app = typer.Typer(help="Run the local web dashboard.")
console = Console()

STATIC_DIR = Path(__file__).resolve().parent / "web"

# Billing endpoints retired when ConductLoop went free (Stripe removed from the app).
_BILLING_GONE = frozenset({
    "/api/billing/checkout-lifetime",
    "/api/billing/checkout-subscription",
    "/api/billing/cancel-subscription",
    "/api/billing/stripe-webhook",
})

# In-memory rate limiter for login attempts (audit 2026-06-23).
# Maps client IP → list of recent failure timestamps (sliding 5-min window).
_LOGIN_FAILS: dict[str, list[float]] = {}

# CSRF token store (audit 2026-06-23): double-submit cookie pattern.
# Maps session token → bound CSRF token. Tokens are issued when a session is
# created and must match the value sent by the client in X-CSRF-Token header
# on every state-changing request.
_CSRF_TOKENS: dict[str, str] = {}


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, indent=2)


def _ts(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Route the stdlib request log (method/path/status) through our logger
        # at DEBUG — off by default at INFO, but captured to the rotating file
        # when OPS_LOG_LEVEL=DEBUG, instead of the old raw-stderr spam.
        try:
            _log.debug("%s %s", self.address_string(), format % args)
        except Exception:
            pass

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight (audit 2026-06-23).
        Marketing site (conductloop.com) needs to call /api/pricing-tier
        cross-origin, which the browser preflights with OPTIONS."""
        # 2026-10-21: CORS only for the allowlisted app origins (origin_guard);
        # foreign origins get no Allow-Origin, so their preflight fails.
        from urllib.parse import urlparse

        from . import origin_guard as _og  # CORS allowlist + cross-site write guard
        cors = _og.cors_headers(self, urlparse(self.path).path)
        self.send_response(204)
        for k, v in cors.items():
            self.send_header(k, v)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def _send(self, status: int, body: bytes, content_type: str = "text/html", headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if content_type.startswith("text/html") and not any(
                k.lower() == "content-security-policy" for k in (headers or {})):
            # Minimal CSP (browser design PR 2). The UI still uses inline
            # <script> and onclick=, so script-src can't be tightened yet.
            # Pages that ship their own strict CSP (sessions, connectors,
            # learning) keep only theirs: one header, not two.
            self.send_header("Content-Security-Policy",
                             "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Content-Length", str(len(body)))
        # Security headers (applied to every response, audit 2026-07-16).
        # - X-Content-Type-Options: nosniff prevents MIME-type sniffing attacks.
        # - X-Frame-Options: DENY prevents clickjacking via iframes.
        # - Referrer-Policy: no-referrer stops the Referer header leaking paths
        #   (which often contain tokens or project slugs) to third-party assets.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        # CORS (2026-10-21, origin_guard): credentials only for the allowlisted
        # app origins; the public pricing/health GETs keep `*` without
        # credentials for the marketing site; any other origin gets nothing.
        from urllib.parse import urlparse as _up

        from . import origin_guard as _og  # CORS allowlist + cross-site write guard
        for k, v in _og.cors_headers(self, _up(getattr(self, "path", "/")).path).items():
            self.send_header(k, v)
        # If the handler queued extra headers (e.g. Set-Cookie from auth), apply them
        extras = getattr(self, "_extra_headers", None)
        if extras:
            for k, v in extras.items():
                self.send_header(k, v)
            self._extra_headers = None  # reset for next request
        # Multiple Set-Cookie headers (session + CSRF): send each on its own
        # line. Headers dict can't dedupe Set-Cookie properly when there are
        # multiple values for the same name.
        cookies = getattr(self, "_pending_cookies", None)
        if cookies:
            for c in cookies:
                self.send_header("Set-Cookie", c)
            self._pending_cookies = None  # reset for next request
        self.end_headers()
        self.wfile.write(body)

    def _send_500(self, exc: Exception, context: str = "") -> None:
        """Top-level 500 handler. Logs the full traceback server-side (visible in
        the server log) and returns a generic body so raw exception text — which
        can leak file paths, SQL, or secrets — never reaches the client.
        Security: replaces the old `self._send(500, str(e).encode())` leak."""
        import time as _time
        err_id = format(int(_time.time() * 1000) & 0xFFFFFF, "06x")
        where = context or getattr(self, "path", "?")
        _log.error("[500 %s] %s %s: %s: %s", err_id, self.command, where,
                   type(exc).__name__, exc, exc_info=exc)
        from .logging_setup import report_exception
        report_exception(exc)
        self._send(
            500,
            json.dumps({"error": "internal server error", "id": err_id}).encode(),
            "application/json",
        )

    def _route(self) -> None:
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # ── Route registry (upgrades #4/#6): registered routes (health, ...)
        # are matched here before the legacy if/elif below. Registering routes
        # in one enumerable place is what turns a "declared but unhandled"
        # endpoint — the /api/health 404 bug — into a testable assertion.
        from . import routes_meta  # noqa: F401  (import registers its routes)
        if _router.dispatch(self, self.command, path):
            return

        # ── Boardroom (2026-07-01): chairman-only section. Routed first so
        # it doesn't get shadowed by the generic `/something` patterns below.
        from . import boardroom as _boardroom

        def _serve_boardroom_html(name: str):
            """Serve a boardroom HTML page by filename. Returns 404 if file missing
            (so 'not yet built' becomes a clean 404, not a 500 traceback)."""
            p = STATIC_DIR / name
            try:
                self._send(200, p.read_bytes(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, f"boardroom page '{name}' not built yet".encode(), "text/plain")

        if path == "/boardroom" or path == "/boardroom/" or path == "/boardroom/inbox" or path == "/boardroom/index.html":
            _serve_boardroom_html("boardroom.html"); return
        if path == "/boardroom/recap" or path == "/boardroom/recap.html":
            _serve_boardroom_html("boardroom-recap.html"); return
        if path == "/boardroom/vault" or path == "/boardroom/vault.html":
            _serve_boardroom_html("boardroom-vault.html"); return
        if path == "/boardroom/directory" or path == "/boardroom/directory.html":
            _serve_boardroom_html("boardroom-directory.html"); return
        if path == "/boardroom/portfolio" or path == "/boardroom/portfolio.html":
            _serve_boardroom_html("boardroom-portfolio.html"); return
        if path == "/boardroom/create" or path == "/boardroom/create.html":
            _serve_boardroom_html("boardroom-create.html"); return
        if path.startswith("/boardroom/ceo/") and path.endswith(".html") is False and path.count("/") == 3:
            # /boardroom/ceo/<id> → boardroom-thread.html (with id in URL so JS can pick it up)
            _serve_boardroom_html("boardroom-thread.html"); return
        if path.startswith("/boardroom/autonomy/") and path.endswith(".html") is False and path.count("/") == 3:
            # /boardroom/autonomy/<id> → boardroom-autonomy.html
            _serve_boardroom_html("boardroom-autonomy.html"); return
        if path == "/api/boardroom/inbox":
            user = self._current_user()
            ok, err = _boardroom.require_chairman(user if user else {"is_admin": 1, "is_chairman": 1})
            if not ok and not self._is_local():
                self._send(403, json.dumps({"error": err}).encode(), "application/json"); return
            self._send(200, json.dumps(_boardroom.inbox(), default=str).encode(), "application/json")
            return
        if path == "/api/boardroom/recap":
            self._send(200, json.dumps(_boardroom.recap(), default=str).encode(), "application/json")
            return
        if path == "/api/boardroom/recap/refresh":
            try: method = self.command
            except Exception: method = "GET"
            if method not in ("GET", "POST"):
                self._send(405, b'{"error":"method not allowed"}', "application/json"); return
            # Force-regenerate the Holding CEO synthesis (bypass cache).
            payload = _boardroom._recap_payload()
            synthesis = _boardroom.recap_synthesis(payload, force=True)
            self._send(200, json.dumps({"synthesis": synthesis, "regenerated_at": int(__import__('time').time())}, default=str).encode(), "application/json")
            return
        if path == "/api/boardroom/vault":
            kind = (qs.get("kind") or [None])[0]
            mid = None
            try: mid = int((qs.get("member_id") or ["0"])[0]) or None
            except Exception: mid = None
            since = None
            try: since = int((qs.get("since") or ["0"])[0]) or None
            except Exception: since = None
            items = _boardroom.vault_feed(limit=100, kind=kind, member_id=mid, since=since)
            kinds = _boardroom.vault_kinds()
            self._send(200, json.dumps({"items": items, "kinds": kinds, "filters": {"kind": kind, "member_id": mid, "since": since}}, default=str).encode(), "application/json")
            return
        if path == "/api/boardroom/directory":
            self._send(200, json.dumps(_boardroom.directory(), default=str).encode(), "application/json")
            return
        if path == "/api/boardroom/portfolio":
            self._send(200, json.dumps(_boardroom.portfolio(), default=str).encode(), "application/json")
            return
        if path == "/api/boardroom/tools":
            self._send(200, json.dumps({"tools": _boardroom.all_tool_catalog()}).encode(), "application/json")
            return
        if path == "/api/boardroom/tools/suggest":
            slug = (qs.get("slug") or [""])[0]
            self._send(200, json.dumps({"tools": _boardroom.suggest_tools_for_project(slug)}).encode(), "application/json")
            return
        if path.startswith("/api/boardroom/ceo/") and path.endswith("/chat") is False and path.count("/") == 4 and path.endswith("/events") is False:
            # GET /api/boardroom/ceo/<id>
            try: ceo_id = int(path.split("/")[4])
            except ValueError:
                self._send(400, b'{"error":"bad ceo id"}', "application/json"); return
            data = _boardroom.ceo_detail(ceo_id)
            if not data:
                self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, json.dumps(data, default=str).encode(), "application/json")
            return
        # ── Per-CEO channel bindings (2026-07-16) ────────────────────────
        if path.startswith("/api/boardroom/ceo/") and path.endswith("/channels") and path.count("/") == 5:
            try: ceo_id = int(path.split("/")[4])
            except ValueError:
                self._send(400, b'{"error":"bad ceo id"}', "application/json"); return
            from . import ceo_channels as _cc
            self._send(200, json.dumps({"items": _cc.list_channels(ceo_id)}, default=str).encode(), "application/json")
            return
        if path.startswith("/api/boardroom/ceo/") and path.endswith("/channels/stats") and path.count("/") == 6:
            try: ceo_id = int(path.split("/")[4])
            except ValueError:
                self._send(400, b'{"error":"bad ceo id"}', "application/json"); return
            from . import ceo_channels as _cc
            chs = _cc.list_channels(ceo_id)
            self._send(200, json.dumps({
                "ceo_id": ceo_id,
                "total": len(chs),
                "enabled": sum(1 for c in chs if c["enabled"]),
                "by_platform": {p: sum(1 for c in chs if c["platform"] == p) for p in ("telegram","discord")},
            }).encode(), "application/json")
            return
        if path.startswith("/api/boardroom/ceo/") and path.endswith("/events") and path.count("/") == 5:
            # GET /api/boardroom/ceo/<id>/events — Server-Sent Events stream
            # of new messages (role=ceo or role=system) since the connection opens.
            # from_id query param lets the client resume from a known position.
            try: ceo_id = int(path.split("/")[4])
            except ValueError:
                self._send(400, b'{"error":"bad ceo id"}', "application/json"); return
            from_id = 0
            try: from_id = int((qs.get("from_id") or ["0"])[0])
            except Exception: from_id = 0
            self._boardroom_ceo_stream(ceo_id, from_id=from_id)
            return
        if path.startswith("/api/boardroom/autonomy/"):
            try: ceo_id = int(path.split("/")[4])
            except ValueError:
                self._send(400, b'{"error":"bad ceo id"}', "application/json"); return
            data = _boardroom.get_autonomy(ceo_id)
            if not data:
                self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, json.dumps(data, default=str).encode(), "application/json")
            return
        if path == "/api/boardroom/summary":
            # one-shot payload used by the activity-bar 🏛️ badge
            inbox_data = _boardroom.inbox()
            blocked = sum(1 for c in inbox_data["items"] if c["status"] == "blocked")
            waiting = sum(1 for c in inbox_data["items"] if c["status"] == "waiting")
            self._send(200, json.dumps({
                "ceos_total": len(inbox_data["items"]),
                "blocked": blocked,
                "waiting": waiting,
            }).encode(), "application/json")
            return
        # ── Starter pack (2026-07-16) — list + seed ─────────────────────────
        if path == "/api/boardroom/starter-pack":
            # Return the curated set of CEOs that ship with a fresh install.
            from .seed_agents import STARTER_PACK
            self._send(200, json.dumps({"items": STARTER_PACK}).encode(), "application/json")
            return
        # ── Telegram bridge (2026-07-01) ─────────────────────────────────
        if path == "/api/telegram/status":
            from . import telegram_bridge as _tg
            self._send(200, json.dumps(_tg.status(), default=str).encode(), "application/json")
            return
        if path == "/api/telegram/chats":
            from . import telegram_bridge as _tg
            bound_only = (qs.get("bound", ["0"])[0] == "1")
            self._send(200, json.dumps({"items": _tg.list_chats(bound_only=bound_only)}, default=str).encode(), "application/json")
            return
        if path == "/api/telegram/chats-bind":
            from . import telegram_bridge as _tg
            try: payload = self._read_json()
            except Exception: payload = {}
            chat_id = payload.get("chat_id")
            try: chat_id_int = int(chat_id) if chat_id is not None else None
            except Exception: chat_id_int = None
            if chat_id_int is None:
                self._send(400, b'{"error":"chat_id required"}', "application/json"); return
            user = self._current_user()
            user_id = (user.get("id") if user else None) or payload.get("user_id") or _boardroom.current_chairman_id()
            out = _tg.bind_chat(chat_id_int, user_id)
            self._send(200, json.dumps(out, default=str).encode(), "application/json")
            return
        # ── Discord bridge (2026-07-01) ───────────────────────────────
        if path == "/api/discord/status":
            from . import discord_bridge as _dc
            self._send(200, json.dumps(_dc.status(), default=str).encode(), "application/json")
            return
        # ── Slack bridge (2026-07-16) ─────────────────────────────────────────
        if path == "/api/slack/status":
            from . import slack_bridge as _sl
            self._send(200, json.dumps(_sl.status(), default=str).encode(), "application/json")
            return
        if path == "/api/telegram/set_webhook":
            # Body: {url, secret?}. When no body is given, we try to read the
            # public URL from NEXT_PUBLIC_SITE_URL or HOST env var.
            from . import telegram_bridge as _tg
            try: payload = self._read_json()
            except Exception: payload = {}
            public_url = (
                (payload.get("url") or "").strip()
                or os.environ.get("NEXT_PUBLIC_SITE_URL", "").strip()
                or os.environ.get("OPS_PUBLIC_URL", "").strip()
                or f"http://localhost:{os.environ.get('OPS_PORT', '7777')}"
            )
            secret = (payload.get("secret") or _tg.WEBHOOK_SECRET or None)
            out = _tg.set_webhook(public_url, secret=secret)
            self._send(200, json.dumps({"ok": out.get("ok"), "result": out, "public_url": public_url}, default=str).encode(), "application/json")
            return
        if path == "/api/telegram/clear_webhook":
            from . import telegram_bridge as _tg
            self._send(200, json.dumps(_tg.delete_webhook(), default=str).encode(), "application/json")
            return
        if path == "/api/telegram/webhook":
            # Telegram → server delivery endpoint.
            from . import telegram_bridge as _tg
            secret_header = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if not _tg._authorized(secret_header):
                self._send(403, b'{"error":"unauthorized"}', "application/json"); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8") if length else ""
                update = json.loads(body) if body else {}
            except Exception as e:
                self._send(400, json.dumps({"error": f"bad json: {e}"}).encode(), "application/json"); return
            try:
                result = _tg.handle_update(update)
                self._send(200, json.dumps(result, default=str).encode(), "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return

        if path == "/license":
            # ConductLoop is free: no license screen. Old bookmarks / builds land home.
            self._send(302, b"", "text/html", {"Location": "/"}); return
        if path == "/api/license/status":
            self._send(200, _json(licensing.status()).encode(), "application/json"); return
        if path in ("/", "/index.html"):
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            self._send(200, html.encode())
        elif path.endswith(".js") and (STATIC_DIR / path.lstrip("/")).exists():
            content = (STATIC_DIR / path.lstrip("/")).read_text(encoding="utf-8")
            self._send(200, content.encode(), "application/javascript; charset=utf-8")
        elif path.startswith("/i18n/") and path.endswith(".json") and (STATIC_DIR / path.lstrip("/")).exists():
            content = (STATIC_DIR / path.lstrip("/")).read_text(encoding="utf-8")
            self._send(200, content.encode(), "application/json; charset=utf-8",
                       headers={"Cache-Control": "public, max-age=300"})
        elif path == "/manifest.json":
            mp = (STATIC_DIR / "manifest.json").read_text(encoding="utf-8")
            self._send(200, mp.encode(), "application/manifest+json")
        elif path == "/sw.js":
            sw = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
            self._send(200, sw.encode(), "application/javascript; charset=utf-8")
        elif path == "/billing":
            bp = (STATIC_DIR / "billing.html").read_text(encoding="utf-8")
            self._send(200, bp.encode(), "text/html; charset=utf-8")
        elif path == "/billing/success":
            # Old Stripe Checkout return URL. ConductLoop is free → home.
            self._send(302, b"", "text/html", {"Location": "/"})
        elif path == "/tour" or path == "/welcome":
            tp = STATIC_DIR / "tour.html"
            if tp.exists():
                self._send(200, tp.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
        elif path == "/changelog" or path == "/changelog.html":
            self._changelog_page()
        elif path == "/api/changelog":
            self._send(200, json.dumps(_changelog_data()).encode(), "application/json")
        elif path.startswith("/note/") and not path.endswith(".html"):
            # Serve the note editor for /note/<id>
            note_path = STATIC_DIR / "note.html"
            if not note_path.exists():
                self._send(404, b"note editor not found", "text/plain"); return
            body = note_path.read_bytes()
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/note.html":
            note_path = STATIC_DIR / "note.html"
            if note_path.exists():
                self._send(200, note_path.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
        elif path.startswith("/icons/"):
            # Serve inline SVG icons
            self._send(404, b"not found")
        elif path == "/api/projects":
            self._send(200, _json(projects.all_projects_summary()).encode(), "application/json")
        elif path == "/api/urls":
            self._send(200, _json(live_urls.latest()).encode(), "application/json")
        elif path == "/pair":
            pp = STATIC_DIR / "pair.html"
            if pp.exists():
                self._send(200, pp.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
        elif path in ("/ai", "/agents", "/runs", "/setup", "/brain"):
            page = STATIC_DIR / (path.lstrip("/") + ".html")
            if page.exists():
                self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
        elif path == "/api/pair/devices":
            # List paired devices — local only.
            if not self._is_local():
                self._send(403, b'{"error":"local only"}', "application/json"); return
            self._send(200, _json(db.list_device_tokens()).encode(), "application/json")
        elif path == "/api/pair/test":
            # Phone calls this with its Bearer token to confirm pairing works.
            u = self._current_user()
            if not u:
                self._send(401, b'{"error":"unauthorized"}', "application/json"); return
            self._send(200, _json({"ok": True, "name": u.get("name")}).encode(), "application/json")
        elif path == "/api/pair/qr.svg":
            # Render a QR for an arbitrary payload — local only.
            if not self._is_local():
                self._send(403, b"local only", "text/plain"); return
            payload = (qs.get("data") or [""])[0]
            if not payload:
                self._send(400, b"missing data", "text/plain"); return
            try:
                import io

                import segno
                buf = io.BytesIO()
                segno.make(payload, error="m").save(buf, kind="svg", scale=6, border=2)
                self._send(200, buf.getvalue(), "image/svg+xml",
                           headers={"Cache-Control": "no-store"})
            except Exception as e:
                self._send(500, f"qr error: {e}".encode(), "text/plain")
        elif path == "/api/project":
            name = (qs.get("name") or [""])[0]
            if not name:
                self._send(400, b'{"error":"missing name"}', "application/json")
                return
            roll = projects.rollup(name)
            if not roll:
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            self._send(200, _json(roll).encode(), "application/json")
        elif path == "/api/playbook":
            name = (qs.get("name") or [""])[0]
            if not name:
                self._send(400, b'{"error":"missing name"}', "application/json")
                return
            data = db.get_playbook(name)
            if not data["entries"]:
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            self._send(200, _json(data).encode(), "application/json")
        elif path == "/api/todos":
            project = qs.get("project", [None])[0]
            only_open = qs.get("all", [None])[0] is None
            limit = int((qs.get("n") or ["80"])[0])
            rows = db.list_todos(project=project, only_open=only_open, limit=limit)
            self._send(200, _json([dict(r) for r in rows]).encode(), "application/json")
        elif path == "/api/urls/changes":
            self._send(200, _json(live_urls.status_changes()).encode(), "application/json")
        elif path == "/api/urls/stream":
            self._live_urls_stream(); return
        elif path == "/api/stream":
            self._unified_stream(); return
        elif path == "/api/search/semantic":
            from . import embeddings
            q = (qs.get("q") or [""])[0]
            if not q:
                self._send(400, b'{"error":"missing q"}', "application/json")
                return
            limit = int((qs.get("n") or ["15"])[0])
            try:
                rows = embeddings.search(q, limit=limit)
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            self._send(200, _json(rows).encode(), "application/json")
        elif path == "/api/brain/ask":
            self._brain_ask()
        elif path == "/api/brain/ask-tools":
            self._brain_ask_tools()
            self._send(200, _json(db.list_conversations(limit=50)).encode(), "application/json")
        elif path == "/api/brain/conversation":
            conv_id = int((qs.get("id") or ["0"])[0])
            data = db.get_conversation(conv_id)
            if not data:
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            self._send(200, _json(data).encode(), "application/json")
        elif path == "/api/brain/conversation/export":
            conv_id = int((qs.get("id") or ["0"])[0])
            md = db.export_conversation_markdown(conv_id)
            if not md:
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            self._send(200, md.encode("utf-8"), "text/markdown; charset=utf-8")
        elif path == "/api/brain/briefing":
            from . import brain
            self._send(200, _json({"text": brain.briefing()}).encode(), "application/json")
        elif path == "/api/brain/providers":
            from . import brain
            self._send(200, _json(brain.list_providers_status()).encode(), "application/json")
        elif path == "/api/brain/templates":
            from . import templates
            self._send(200, _json(templates.list_templates()).encode(), "application/json")
        elif path == "/api/brain/memory":
            self._send(200, _json(db.memory_list()).encode(), "application/json")
        elif path == "/api/brain/stream":
            self._brain_stream()
            return  # SSE response handles its own headers
        elif path == "/api/snapshot/diff":
            from . import snapshots
            d = snapshots.diff(day=qs.get("day", [None])[0])
            self._send(200, _json(d).encode(), "application/json")
        elif path == "/api/snapshot/weekly":
            from . import snapshots
            self._send(200, _json({"text": snapshots.render_weekly()}).encode(), "application/json")
        elif path.startswith("/api/tags/by/"):
            tag = path[len("/api/tags/by/"):]
            self._send(200, _json(db.docs_by_tag(tag)).encode(), "application/json")
        elif path == "/api/user_todos":
            self._user_todos_list()
        elif path.startswith("/api/user_todos/"):
            self._user_todo_action(path[len("/api/user_todos/"):])
        elif path == "/api/ideas":
            self._ideas_list()
        elif path == "/api/docs/create":
            self._docs_create()
        elif path == "/api/activity_feed":
            from urllib.parse import parse_qs, urlparse

            from . import db as _db
            qs = parse_qs(urlparse(self.path).query)
            try: limit = int((qs.get("limit") or ["50"])[0])
            except Exception: limit = 50
            self._send(200, _json(_db.recent_activity(limit=limit)).encode(), "application/json")
        elif path == "/api/brain/search":
            from urllib.parse import parse_qs, urlparse

            from . import db as _db
            qs = parse_qs(urlparse(self.path).query)
            q = (qs.get("q") or [""])[0].strip()
            rows = _db.list_brain_conversations_text(limit=500) if not q else _db.list_brain_conversations_text(limit=500)
            # simple filter
            if q:
                ql = q.lower()
                rows = [r for r in rows if ql in (r.get("content") or "").lower() or ql in (r.get("title") or "").lower()]
            self._send(200, _json({"rows": rows[:50]}).encode(), "application/json")
        elif path == "/api/calendar":
            from urllib.parse import parse_qs, urlparse

            from . import db as _db
            qs = parse_qs(urlparse(self.path).query)
            from datetime import datetime
            today = datetime.now().date()
            start = int(datetime(today.year, today.month, 1).timestamp())
            end = int(datetime(today.year, today.month + 1, 1).timestamp()) if today.month < 12 else int(datetime(today.year + 1, 1, 1).timestamp())
            with _db.db() as conn:
                rows = conn.execute(
                    "SELECT id, content, project_key, due_date, status, priority, completed_at "
                    "FROM user_todos WHERE due_date >= ? AND due_date < ? "
                    "ORDER BY due_date, priority DESC, id",
                    (start, end),
                ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                if d.get("due_date"):
                    d["due_date_iso"] = datetime.fromtimestamp(d["due_date"]).strftime("%Y-%m-%d")
                out.append(d)
            self._send(200, _json({"rows": out, "month": today.strftime("%Y-%m")}).encode(), "application/json")
        elif path == "/api/agents/activity":
            self._agent_activity()
            return
        elif path == "/api/agents/activity/stream":
            self._activity_stream()
            return
        elif path == "/api/approvals":
            self._approvals()
            return
        elif path.startswith("/api/webhook/"):
            token = path[len("/api/webhook/"):]
            self._webhook_dispatch(token)
            return
        elif path.startswith("/api/agents/"):
            from urllib.parse import parse_qs, urlparse
            tail = path[len("/api/agents/"):]
            parts = tail.split("/")
            try: mid = int(parts[0])
            except (ValueError, IndexError):
                self._send(400, b'{"error":"bad id"}', "application/json"); return
            if len(parts) == 1:
                m = db.get_member(mid)
                if not m: self._send(404, b'{"error":"not found"}', "application/json"); return
                self._send(200, _json(m).encode(), "application/json")
                return
            elif len(parts) == 2 and parts[1] == "memory":
                self._agent_memory_handler(mid); return
            elif len(parts) == 3 and parts[1] == "memory":
                try: mem_id = int(parts[2])
                except ValueError: self._send(400, b'{"error":"bad memory id"}', "application/json"); return
                self._agent_memory_action(mid, mem_id); return
            elif len(parts) == 2 and parts[1] == "triggers":
                self._agent_trigger_handler(mid); return
            elif len(parts) == 3 and parts[1] == "triggers":
                try: tid = int(parts[2])
                except ValueError: self._send(400, b'{"error":"bad trigger id"}', "application/json"); return
                self._agent_trigger_action(mid, tid); return
            elif len(parts) == 2 and parts[1] == "logs":
                self._agent_logs_handler(mid); return
            elif len(parts) == 2 and parts[1] == "wake":
                self._agent_wake_handler(mid); return
            elif len(parts) == 2 and parts[1] == "stream":
                self._agent_stream(mid); return
            elif len(parts) == 2 and parts[1] == "tools":
                self._agent_tools(mid); return
            self._send(404, b'{"error":"unknown agent subpath"}', "application/json"); return
        elif path == "/api/revenue/sources":
            self._revenue_sources(); return
        elif path == "/api/revenue/events":
            self._revenue_events(); return
        elif path == "/api/revenue/summary":
            self._revenue_summary(); return
        elif path == "/api/revenue/daily":
            self._revenue_daily(); return
        elif path == "/api/revenue/seed-mock":
            self._revenue_seed_mock(); return
        elif path == "/api/sentry/projects":
            self._sentry_projects(); return
        elif path == "/api/sentry/issues":
            self._sentry_issues(); return
        elif path == "/api/sentry/summary":
            self._sentry_summary(); return
        elif path == "/api/sentry/seed-mock":
            self._sentry_seed_mock(); return
        elif path.startswith("/api/sentry/projects/"):
            tail = path[len("/api/sentry/projects/"):]
            parts = tail.split("/")
            try: sid = int(parts[0])
            except (ValueError, IndexError):
                self._send(400, b'{"error":"bad id"}', "application/json"); return
            if len(parts) == 1:
                self._sentry_project_action(sid); return
            if len(parts) == 2 and parts[1] == "test":
                self._sentry_project_test(sid); return
            if len(parts) == 2 and parts[1] == "poll":
                self._sentry_project_poll(sid); return
            self._send(404, b'{"error":"unknown subpath"}', "application/json"); return
        elif path == "/api/revenue/margin":
            self._revenue_margin(); return
        elif path == "/api/revenue/forecast":
            self._revenue_forecast_api(); return
        elif path == "/api/ga/forecast":
            self._ga_forecast_api(); return
        elif path == "/api/anomalies":
            self._anomalies_api(); return
        elif path == "/api/digest":
            self._digest_api(); return
        elif path == "/api/digest/ai-summary":
            self._digest_ai_summary_api(); return
        elif path == "/api/status.json":
            self._status_json(); return
        elif path == "/status":
            self._status_page(); return
        elif path == "/api/revenue/cohorts":
            self._cohorts_api(); return
        elif path == "/api/cost/seed-mock":
            self._cost_seed_mock(); return
        elif path == "/api/ga/sites":
            self._ga_sites(); return
        elif path == "/api/status.json":
            self._status_json(); return
        elif path == "/status":
            self._status_page(); return
        elif path == "/api/ga/metrics":
            self._ga_metrics(); return
        elif path == "/api/ga/summary":
            self._ga_summary(); return
        elif path == "/api/ga/daily":
            self._ga_daily(); return
        elif path == "/api/gsc/sites":
            self._gsc_sites(); return
        elif path == "/api/gsc/summary":
            self._gsc_summary(); return
        elif path == "/api/gsc/daily":
            self._gsc_daily(); return
        elif path == "/api/risk":
            from urllib.parse import parse_qs, urlparse

            from .indexers import risk as risk_mod
            qs = parse_qs(urlparse(self.path).query)
            project = (qs.get("project") or [None])[0]
            result = risk_mod.run_assessment(project_key=project)
            self._send(200, json.dumps(result, default=str).encode(), "application/json")
            return
            self._ga_daily(); return
        elif path == "/api/ga/seed-mock":
            self._ga_seed_mock(); return
        elif path.startswith("/api/ga/sites/"):
            tail = path[len("/api/ga/sites/"):]
            parts = tail.split("/")
            try: sid = int(parts[0])
            except (ValueError, IndexError):
                self._send(400, b'{"error":"bad id"}', "application/json"); return
            if len(parts) == 1:
                self._ga_site_action(sid); return
            if len(parts) == 2 and parts[1] == "test":
                self._ga_site_test(sid); return
            if len(parts) == 2 and parts[1] == "poll":
                self._ga_site_poll(sid); return
            self._send(404, b'{"error":"unknown subpath"}', "application/json"); return
        elif path.startswith("/api/revenue/sources/"):
            tail = path[len("/api/revenue/sources/"):]
            parts = tail.split("/")
            try: sid = int(parts[0])
            except (ValueError, IndexError):
                self._send(400, b'{"error":"bad id"}', "application/json"); return
            if len(parts) == 1:
                self._revenue_source_action(sid); return
            if len(parts) == 2 and parts[1] == "test":
                self._revenue_source_test(sid); return
            if len(parts) == 2 and parts[1] == "poll":
                self._revenue_source_poll(sid); return
            self._send(404, b'{"error":"unknown subpath"}', "application/json"); return
        elif path == "/api/connections/catalog":
            self._connections_catalog(); return
        elif path == "/api/connections":
            self._connections(); return
        elif path.startswith("/api/connections/"):
            tail = path[len("/api/connections/"):]
            parts = tail.split("/")
            try: cid = int(parts[0])
            except (ValueError, IndexError):
                self._send(400, b'{"error":"bad id"}', "application/json"); return
            if len(parts) == 1:
                self._connection_action(cid); return
            if len(parts) == 2 and parts[1] == "test":
                self._connection_test(cid); return
            if len(parts) == 2 and parts[1] == "tools":
                self._connection_tools(cid); return
            self._send(404, b'{"error":"unknown subpath"}', "application/json"); return
        elif path == "/api/mcp/servers":
            self._mcp_servers(); return
        elif path.startswith("/api/mcp/servers/"):
            tail = path[len("/api/mcp/servers/"):]
            parts = tail.split("/")
            name = parts[0]
            if len(parts) == 1:
                self._mcp_server_action(name); return
            if len(parts) == 2 and parts[1] == "discover":
                self._mcp_discover(name); return
            self._send(404, b'{"error":"unknown subpath"}', "application/json"); return
        elif path == "/api/teams":
            from urllib.parse import parse_qs, urlparse

            from . import db as _db
            qs = parse_qs(urlparse(self.path).query)
            _db.seed_default_teams()
            self._send(200, _json(_db.list_teams()).encode(), "application/json")
        elif path.startswith("/api/teams/"):
            from urllib.parse import parse_qs, urlparse

            from . import db as _db
            qs = parse_qs(urlparse(self.path).query)
            try: tid = int(path[len("/api/teams/"):])
            except ValueError: self._send(400, b'{"error":"bad id"}', "application/json"); return
            t = _db.get_team(tid)
            if not t: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, _json(t).encode(), "application/json")
        elif path == "/api/members":
            from . import db as _db
            self._send(200, _json(_db.list_members()).encode(), "application/json")
        elif path.startswith("/api/members/"):
            from . import db as _db
            try: mid = int(path[len("/api/members/"):])
            except ValueError: self._send(400, b'{"error":"bad id"}', "application/json"); return
            m = _db.get_member(mid)
            if not m: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, _json(m).encode(), "application/json")
        elif path == "/api/teams":
            from . import db as _db
            _db.seed_default_teams()
            self._send(200, _json(_db.list_teams()).encode(), "application/json")
        elif path.startswith("/api/teams/"):
            from . import db as _db
            try: tid = int(path[len("/api/teams/"):])
            except ValueError: self._send(400, b'{"error":"bad id"}', "application/json"); return
            t = _db.get_team(tid)
            if not t: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, _json(t).encode(), "application/json")
        elif path == "/api/members":
            from . import db as _db
            self._send(200, _json(_db.list_members()).encode(), "application/json")
        elif path.startswith("/api/members/"):
            from . import db as _db
            try: mid = int(path[len("/api/members/"):])
            except ValueError: self._send(400, b'{"error":"bad id"}', "application/json"); return
            m = _db.get_member(mid)
            if not m: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, _json(m).encode(), "application/json")
        elif path == "/api/code_todos":
            from . import db as _db
            qs = parse_qs(urlparse(self.path).query)
            project = (qs.get("project") or [None])[0]
            priority = (qs.get("priority") or [None])[0]
            only_open = (qs.get("all") or [None])[0] is None
            try: limit = int((qs.get("limit") or ["200"])[0])
            except Exception: limit = 200
            rows = _db.list_code_todos(project_key=project, priority=priority,
                                       only_open=only_open, limit=limit)
            self._send(200, _json({"rows": rows, "summary": _db.code_todo_summary()}).encode(), "application/json")
        elif path == "/api/code_todos/done" and self.command == "POST":
            from . import db as _db
            try:
                payload = self._read_json()
                _db.mark_code_todo_done(int(payload["id"]), done=bool(payload.get("done", True)))
                self._send(200, b'{"ok":true}', "application/json")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
        elif path == "/api/decisions":
            from . import db as _db
            qs = parse_qs(urlparse(self.path).query)
            project = (qs.get("project") or [None])[0]
            search = (qs.get("q") or [None])[0]
            try: limit = int((qs.get("limit") or ["100"])[0])
            except Exception: limit = 100
            rows = _db.list_decisions(project_key=project, search=search, limit=limit)
            self._send(200, _json({"rows": rows, "summary": _db.decisions_summary()}).encode(), "application/json")
        elif path == "/api/decision":
            from . import db as _db
            try:
                payload = self._read_json()
                did = _db.create_decision(
                    project_key=payload.get("project_key"),
                    title=payload["title"],
                    context=payload.get("context", ""),
                    decision=payload["decision"],
                    consequences=payload.get("consequences"),
                    links=payload.get("links"),
                    tags=payload.get("tags"),
                )
                self._send(200, json.dumps({"id": did, "ok": True}).encode(), "application/json")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
        elif path.startswith("/api/decision/"):
            from . import db as _db
            try:
                did = int(path[len("/api/decision/"):])
            except ValueError:
                self._send(400, b'{"error":"bad id"}', "application/json"); return
            if self.command == "DELETE":
                ok = _db.delete_decision(did)
                self._send(200, json.dumps({"ok": ok}).encode(), "application/json")
            elif self.command == "PUT":
                try:
                    payload = self._read_json()
                    ok = _db.update_decision(did, **payload)
                    self._send(200, json.dumps({"ok": ok}).encode(), "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            else:
                d = _db.get_decision(did)
                if not d:
                    self._send(404, b'{"error":"not found"}', "application/json"); return
                self._send(200, _json(d).encode(), "application/json")
        elif path == "/api/costs":
            from . import db as _db
            qs = parse_qs(urlparse(self.path).query)
            period = (qs.get("period") or [None])[0]
            project = (qs.get("project") or [None])[0]
            provider = (qs.get("provider") or [None])[0]
            self._send(200, _json({"rows": _db.list_costs(period=period, project_key=project, provider=provider),
                                   "summary": _db.costs_summary(period=period)}).encode(), "application/json")
        elif path == "/api/cost":
            from . import db as _db
            if self.command == "POST":
                try:
                    payload = self._read_json()
                    cid = _db.add_cost(
                        provider=payload["provider"],
                        amount_cents=int(payload.get("amount_cents") or round(float(payload["amount"]) * 100)),
                        currency=payload.get("currency", "EUR"),
                        project_key=payload.get("project_key"),
                        category=payload.get("category", "infra"),
                        description=payload.get("description"),
                        period=payload.get("period"),
                    )
                    self._send(200, json.dumps({"id": cid, "ok": True}).encode(), "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            else:
                self._send(405, b'{"error":"use POST"}', "application/json")
        elif path.startswith("/api/cost/") and self.command == "DELETE":
            from . import db as _db
            try:
                cid = int(path[len("/api/cost/"):])
            except ValueError:
                self._send(400, b'{"error":"bad id"}', "application/json"); return
            ok = _db.delete_cost(cid)
            self._send(200, json.dumps({"ok": ok}).encode(), "application/json")
        elif path == "/api/templates":
            from . import db as _db
            self._send(200, _json({"rows": _db.list_templates()}).encode(), "application/json")
        elif path.startswith("/api/i18n/") and path.endswith(".json"):
            self._i18n_serve(path); return
        elif path == "/api/auth/signup":
            self._auth_signup(); return
        elif path == "/api/auth/login":
            self._auth_login(); return
        elif path == "/api/auth/logout":
            self._auth_logout(); return
        elif path == "/api/auth/me":
            self._auth_me(); return
        elif path == "/api/billing/plans":
            self._billing_plans(); return
        elif path in _BILLING_GONE:
            self._billing_gone(); return
        elif path == "/api/billing/entitlement":
            self._billing_entitlement(); return
        elif path.startswith("/api/templates/") and path.endswith("/instantiate"):
            from . import db as _db
            if self.command != "POST":
                self._send(405, b'{"error":"use POST"}', "application/json"); return
            try:
                tail = path[len("/api/templates/"):-len("/instantiate")]
                # Either int id or "by-key/<key>"
                if tail.startswith("by-key/"):
                    tpl = _db.get_template_by_key(tail[len("by-key/"):])
                    if not tpl:
                        self._send(404, b'{"error":"not found"}', "application/json"); return
                    payload = self._read_json() if int(self.headers.get("Content-Length", 0)) > 0 else {}
                    ids = _db.instantiate_template(tpl["id"], project_key=payload.get("project_key"))
                else:
                    tid = int(tail)
                    payload = self._read_json() if int(self.headers.get("Content-Length", 0)) > 0 else {}
                    ids = _db.instantiate_template(tid, project_key=payload.get("project_key"))
                self._send(200, json.dumps({"ok": True, "ids": ids}).encode(), "application/json")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
        elif path == "/api/github":
            from . import db as _db
            qs = parse_qs(urlparse(self.path).query)
            repo = (qs.get("repo") or [None])[0]
            item_type = (qs.get("type") or [None])[0]
            state = (qs.get("state") or [None])[0]
            try: limit = int((qs.get("limit") or ["100"])[0])
            except Exception: limit = 100
            rows = _db.list_github_items(repo=repo, item_type=item_type, state=state, limit=limit)
            self._send(200, _json({"rows": rows, "summary": _db.github_summary()}).encode(), "application/json")
        elif path == "/api/github/sync" and self.command == "POST":
            from . import db as _db
            from .indexers import github as gh
            try:
                counters = gh.run()
                self._send(200, _json({"ok": True, "counters": counters,
                                        "summary": _db.github_summary()}).encode(), "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
        elif path == "/api/url_changes":
            from . import db as _db
            qs = parse_qs(urlparse(self.path).query)
            try: hours = int((qs.get("hours") or ["168"])[0])
            except Exception: hours = 168
            try: limit = int((qs.get("limit") or ["50"])[0])
            except Exception: limit = 50
            rows = _db.recent_url_changes(hours=hours, limit=limit)
            self._send(200, _json({"rows": rows}).encode(), "application/json")
        elif path == "/api/voice/notes":
            from . import db as _db
            qs = parse_qs(urlparse(self.path).query)
            project = (qs.get("project") or [None])[0]
            try: limit = int((qs.get("limit") or ["50"])[0])
            except Exception: limit = 50
            self._send(200, _json({"rows": _db.list_voice_notes(project_key=project, limit=limit)}).encode(), "application/json")
        elif path == "/api/voice/transcribe":
            self._voice_transcribe()
        elif path == "/api/voice/available":
            from . import voice
            self._send(200, json.dumps({"available": voice.is_available()}).encode(), "application/json")
        elif path == "/api/project_report":
            from urllib.parse import parse_qs, urlparse

            from . import db as _db
            from . import projects as _projects_mod
            qs = parse_qs(urlparse(self.path).query)
            key = (qs.get("key") or [""])[0]
            if not key:
                self._send(400, b'{"error":"key required"}', "application/json")
                return
            d = _projects_mod.rollup(key)
            if not d:
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            p = d["project"]
            md = []
            md.append(f"# {p['name']} — Status report")
            md.append("")
            md.append(f"_Auto-generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_")
            md.append("")
            md.append("## Phase")
            md.append("")
            md.append(f"**{p.get('is_parent') and 'Parent project' or 'Standalone project'}**")
            if p.get("children"):
                md.append("")
                md.append("Sub-products:")
                for c in p["children"]:
                    md.append(f"- `{c}`")
            md.append("")
            md.append("## URLs")
            md.append("")
            if d["urls"]:
                ok = sum(1 for u in d["urls"] if u["ok"])
                md.append(f"**{ok}/{len(d['urls'])} up**")
                md.append("")
                for u in d["urls"]:
                    icon = "🟢" if u["ok"] else "🔴"
                    md.append(f"- {icon} **{u['name']}** ({u['status_code']}) — {u['url']}")
            else:
                md.append("_no URLs configured_")
            md.append("")
            md.append("## Repositories")
            md.append("")
            if d["repos"]:
                for r in d["repos"]:
                    md.append(f"- 📁 **{r['name']}** — `{r['path']}`")
            else:
                md.append("_no repos on disk_")
            md.append("")
            md.append(f"## Mentioned in {len(d['docs'])} docs")
            md.append("")
            for doc in d["docs"][:15]:
                md.append(f"- [{doc['title'] or doc['path'].split('/')[-1]}]({doc['path']})")
            md.append("")
            self._send(200, ("\n".join(md)).encode("utf-8"), "text/markdown; charset=utf-8")
        elif path.startswith("/api/ideas/"):
            self._idea_action(path[len("/api/ideas/"):])
        elif path == "/api/skills":
            from urllib.parse import parse_qs, urlparse

            from .indexers import skills as skills_idx
            qs = parse_qs(urlparse(self.path).query)
            rows = skills_idx.list_skills(
                category=qs.get("category", [None])[0],
                subcategory=qs.get("subcategory", [None])[0],
                query=qs.get("q", [None])[0],
            )
            self._send(200, _json({"rows": rows, "summary": skills_idx.skills_summary()}).encode(), "application/json")
        elif path.startswith("/api/skills/by/"):
            from urllib.parse import urlparse

            from .indexers import skills as skills_idx
            key = path[len("/api/skills/by/"):]
            rows = skills_idx.list_skills(query=key)
            self._send(200, _json({"rows": rows, "key": key}).encode(), "application/json")
        elif path == "/api/graph":
            limit = int((qs.get("limit") or ["500"])[0])
            self._send(200, _json(db.build_relations(limit=limit)).encode(), "application/json")
        elif path == "/api/webhooks/status":
            from . import webhooks
            self._send(200, _json(webhooks.status()).encode(), "application/json")
        elif path == "/api/webhooks/test":
            from . import webhooks
            results = webhooks.notify("test notification from ops-dashboard", severity="info")
            self._send(200, _json(results).encode(), "application/json")
        elif path == "/api/find/recent":
            since_q = (qs.get("when") or ["esta_semana"])[0]
            date_range = db.parse_date_range(since_q) or (0, int(time.time()))
            with db.db() as conn:
                rows = conn.execute(
                    "SELECT id, source, path, title, mtime FROM documents "
                    "WHERE mtime >= ? AND mtime <= ? ORDER BY mtime DESC LIMIT ?",
                    (date_range[0], date_range[1], int((qs.get("n") or ["30"])[0])),
                ).fetchall()
            self._send(200, _json([dict(r) for r in rows]).encode(), "application/json")
        elif path.startswith("/api/notes/") and path.endswith("/ai-action"):
            try:
                doc_id = int(path.split("/")[3])
                self._notes_ai_action(doc_id)
            except (ValueError, IndexError):
                self._send(400, b'{"error":"bad doc id"}', "application/json")
            return
        elif path.startswith("/api/notes/") and path.endswith("/save"):
            try:
                doc_id = int(path.split("/")[3])
                self._notes_save(doc_id)
            except (ValueError, IndexError):
                self._send(400, b'{"error":"bad doc id"}', "application/json")
            return
        elif path.startswith("/api/notes/") and "/ai-action" not in path and "/save" not in path and "/backlinks" not in path and "/links" not in path:
            try:
                doc_id = int(path.split("/")[3])
                self._notes_get(doc_id)
            except (ValueError, IndexError):
                self._send(400, b'{"error":"bad doc id"}', "application/json")
            return
        elif path.startswith("/api/notes/") and path.endswith("/backlinks"):
            try:
                doc_id = int(path.split("/")[3])
                self._notes_backlinks(doc_id)
            except (ValueError, IndexError):
                self._send(400, b'{"error":"bad doc id"}', "application/json")
            return
        elif path.startswith("/api/notes/") and path.endswith("/links"):
            try:
                doc_id = int(path.split("/")[3])
                self._notes_links(doc_id)
            except (ValueError, IndexError):
                self._send(400, b'{"error":"bad doc id"}', "application/json")
            return
        elif path == "/api/backlinks/stats":
            self._send(200, json.dumps(db.count_links()).encode(), "application/json")
            return
        elif path == "/api/find":
            exact = (qs.get("path") or [""])[0]
            if exact:
                # Exact path lookup (vault clicks): a URL-backed web clip has no
                # usable basename to search for.
                self._send(200, _json(db.documents_by_path(exact)).encode(), "application/json")
                return
            q = (qs.get("q") or [""])[0]
            if not q:
                self._send(400, b'{"error":"missing q"}', "application/json")
                return
            limit = int((qs.get("n") or ["15"])[0])
            mode = (qs.get("mode") or ["text"])[0]   # 'text' | 'semantic' | 'hybrid'
            fuzzy = (qs.get("fuzzy") or ["1"])[0] not in ("0", "false", "")
            # Project prefix filter (e.g. ":rentcla verifactu")
            project_prefix, q = db.parse_project_prefix(q)
            # Date range filter (e.g. "qué hice ayer")
            date_range = db.parse_date_range(q)
            since_ts = date_range[0] if date_range else None
            until_ts = date_range[1] if date_range else None
            if mode == "semantic":
                try:
                    from . import embeddings
                    if not q and project_prefix:
                        # Just project filter, no text
                        with db.db() as conn:
                            rows = [dict(r) for r in conn.execute(
                                "SELECT id, source, path, title, mtime FROM documents "
                                "WHERE LOWER(path) LIKE ? OR LOWER(IFNULL(title, '')) LIKE ? "
                                "ORDER BY mtime DESC LIMIT ?",
                                (f"%{project_prefix}%", f"%{project_prefix}%", limit)
                            ).fetchall()]
                    else:
                        rows = embeddings.search(q, limit=limit * 2)
                        if project_prefix:
                            rows = [r for r in rows if project_prefix in r["path"].lower()
                                    or project_prefix in (r["title"] or "").lower()]
                    out = [dict(r) for r in rows[:limit]]
                    self._send(200, _json(out).encode(), "application/json")
                    return
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
                    return
            if not q and project_prefix:
                # Project-only search: get all docs that mention the project alias
                with db.db() as conn:
                    raw = conn.execute(
                        "SELECT id, source, path, title, mtime, content, metadata "
                        "FROM documents ORDER BY mtime DESC LIMIT 2000"
                    ).fetchall()
                rows = []
                for r in raw:
                    if project_prefix in r["path"].lower() or project_prefix in (r["title"] or "").lower():
                        rows.append({"id": r["id"], "source": r["source"], "path": r["path"],
                                     "title": r["title"], "mtime": r["mtime"], "excerpt": "",
                                     "metadata": r["metadata"]})
                rows = rows[:limit]
            else:
                rows = db.search(q, limit=limit * 3 if fuzzy else limit, fuzzy=fuzzy)
                if project_prefix:
                    rows = [r for r in rows if project_prefix in r["path"].lower()
                            or project_prefix in (r["title"] or "").lower()]
            if since_ts is not None:
                rows = [r for r in rows if r["mtime"] is not None and since_ts <= r["mtime"] <= until_ts]
            out = [dict(r) if not isinstance(r, dict) else r for r in rows[:limit]]
            self._send(200, _json(out).encode(), "application/json")
        elif path == "/api/orgchart":
            self._send(200, _json(self._orgchart()).encode(), "application/json")
        elif path == "/api/hierarchy":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            root = (qs.get("root") or [None])[0]
            root_id = int(root) if root else None
            tree = db.get_org_tree(root_id=root_id)
            tree["balance_suggestions"] = db.hierarchy_balance_suggestions()
            self._send(200, _json(tree).encode(), "application/json")
        elif path == "/api/hierarchy/balance-suggestions":
            self._send(200, _json(db.hierarchy_balance_suggestions()).encode(), "application/json")
        elif path == "/api/brain/grafo":
            self._send(200, _json(self._brain_grafo()).encode(), "application/json")
        elif path == "/api/tasks" or (path.startswith("/api/tasks/") and re.match(r"^/api/tasks/\d+(?:/runs)?/?$", path)):
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            project = (qs.get("project") or [None])[0]
            status  = (qs.get("status")  or [None])[0]
            try: limit = int((qs.get("limit") or ["100"])[0])
            except Exception: limit = 100
            tail = path[len("/api/tasks"):]
            # /api/tasks/123 or /api/tasks/123/runs
            m = re.match(r"^/(\d+)(?:/(runs))?/?$", tail)
            if m:
                task_id = int(m.group(1))
                if m.group(2) == "runs":
                    self._send(200, _json(db.list_runs(task_id)).encode(), "application/json")
                else:
                    task = db.get_task(task_id)
                    if not task:
                        self._send(404, b'{"error":"not found"}', "application/json")
                        return
                    task["runs"] = db.list_runs(task_id)
                    self._send(200, _json(task).encode(), "application/json")
            else:
                rows = db.list_tasks(project_key=project, status=status, limit=limit)
                self._send(200, _json({"rows": rows, "summary": db.task_stats()}).encode(), "application/json")
        elif re.match(r"^/api/tasks/\d+/run$", path):
            # /run is POST-only (start/re-run a task). GET returns 405.
            if self.command != "POST":
                self._send(405, b'{"error":"use POST"}', "application/json")
                return
            m = re.match(r"^/api/tasks/(\d+)/run$", path)
            if m:
                from . import orchestrator
                tid = int(m.group(1))
                qs = parse_qs(urlparse(self.path).query)
                sync = (qs.get("sync") or ["1"])[0] in ("1", "true", "yes")
                result = orchestrator.run_task(tid, sync=sync)
                _pe.send_result(self, result, 200 if result.get("ok") else 400, _json(result).encode())
            else:
                self._send(404, b"not found")
        elif re.match(r"^/api/tasks/\d+/stream$", path):
            self._task_stream(path)
        elif re.match(r"^/api/tasks/\d+/approve$", path):
            # /approve is POST-only.
            if self.command != "POST":
                self._send(405, b'{"error":"use POST"}', "application/json")
                return
            m = re.match(r"^/api/tasks/(\d+)/approve$", path)
            if m:
                from . import orchestrator
                task_id = int(m.group(1))
                result = orchestrator.approve_run(task_id=task_id)
                status = 200 if result.get("ok") else 400
                self._send(status, _json(result).encode(), "application/json")
            else:
                self._send(404, b"not found")
        else:
            self._send(404, b"not found")

    # GET endpoints reachable without auth from a NON-local origin (phone/relay).
    # Localhost (the desktop webview) is always allowed. Everything else over the
    # network requires a paired-device Bearer token or a logged-in session.
    _PUBLIC_GET_PATHS = {"/api/health", "/health"}

    def _get_allowed(self) -> bool:
        if self._is_local():
            return True
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path in self._PUBLIC_GET_PATHS:
            return True
        return bool(self._current_user())

    def do_GET(self) -> None:
        from urllib.parse import urlparse
        try:
            if not self._get_allowed():
                self._send(401, b'{"error":"authentication required"}', "application/json")
                return
            # GETs with side effects get the same Origin check as writes.
            from . import origin_guard as _og  # CORS allowlist + cross-site write guard
            if not _og.guard(self, "GET", urlparse(self.path).path):
                return
            self._route()
        except Exception as e:
            self._send_500(e)

    def _brain_grafo(self) -> dict:
        """Return a radial graph of the brain's contents.

        Returns:
          {
            center: {id, label, type},
            sections: [
              {id, label, type, color, count, items: [{id, label, type, sub, count}]}
            ],
            edges: [{from, to}],   # from = section id or center; to = item id
          }
        """
        from . import projects as proj_registry
        from .indexers import live_urls

        nodes: list[dict] = []
        edges: list[dict] = []
        seen: set[str] = set()

        def add(node_id: str, label: str, type_: str, sub: str = "", count: int = 0) -> None:
            if node_id in seen: return
            seen.add(node_id)
            nodes.append({"id": node_id, "label": label, "type": type_, "sub": sub, "count": count})

        # Center node
        add("center", "Brain", "center")

        # Section 1: Projects
        for p in proj_registry.PROJECTS:
            add(f"proj:{p.key}", p.name, "project", "Projects", len(getattr(p, "aliases", []) or []))
            edges.append({"from": "center", "to": f"proj:{p.key}"})

        # Section 2: Recent documents (top 20 by mtime)
        with db.db() as conn:
            docs = conn.execute(
                "SELECT id, source, path, title, mtime FROM documents ORDER BY mtime DESC LIMIT 20"
            ).fetchall()
        for d in docs:
            title = (d["title"] or d["path"].split("/")[-1] or "(untitled)")[:50]
            doc_id = f"doc:{d['id']}"
            add(doc_id, title, d["source"] or "doc", "Recent", 0)
            edges.append({"from": "center", "to": doc_id})

        # Section 3: Open tasks
        tasks = db.list_tasks(status="running", limit=10) + db.list_tasks(status="queued", limit=10)
        # also include done tasks from last 7 days
        with db.db() as conn:
            from time import time as _now
            done_recent = conn.execute(
                "SELECT * FROM tasks WHERE status = 'done' AND updated_at > ? ORDER BY updated_at DESC LIMIT 10",
                (_now() - 7 * 86400,),
            ).fetchall()
        for t in list(tasks) + [dict(r) for r in done_recent]:
            t_id = f"task:{t['id']}"
            add(t_id, t["title"][:50], "task", "Tasks", 0)
            edges.append({"from": "center", "to": t_id})

        # Section 4: URLs (live)
        try:
            url_snap = live_urls.latest()
        except Exception:
            url_snap = []
        for u in url_snap:
            ok = u.get("ok", False)
            u_id = f"url:{u['name']}"
            add(u_id, u["name"], "url_" + ("ok" if ok else "down"), "URLs", 0)
            edges.append({"from": "center", "to": u_id})

        # Section 5: Open user todos
        with db.db() as conn:
            todos = conn.execute(
                "SELECT id, content, project_key FROM user_todos WHERE status = 'open' ORDER BY priority DESC, created_at DESC LIMIT 10"
            ).fetchall()
        for t in todos:
            t_id = f"todo:{t['id']}"
            add(t_id, t["content"][:50], "todo", "To do", 0)
            edges.append({"from": "center", "to": t_id})

        # Section 6: People (top active team members)
        with db.db() as conn:
            people = conn.execute(
                "SELECT id, name, type, role, level FROM team_members WHERE status = 'active' ORDER BY level LIMIT 12"
            ).fetchall()
        for p in people:
            p_id = f"person:{p['id']}"
            add(p_id, p["name"], p["type"] or "person", "People", 0)
            edges.append({"from": "center", "to": p_id})

        # Cross-links: project -> recent docs that mention it
        for d in docs:
            doc_id = f"doc:{d['id']}"
            dpath = (d["path"] or "").lower()
            for p in proj_registry.PROJECTS:
                if p.key.lower() in dpath:
                    edges.append({"from": f"proj:{p.key}", "to": doc_id})

        # Cross-links: project -> its tasks
        for t in list(tasks):
            if t.get("project_key"):
                edges.append({"from": f"proj:{t['project_key']}", "to": f"task:{t['id']}"})

        # Cross-links: project -> its URLs (by name match)
        for u in url_snap:
            for p in proj_registry.PROJECTS:
                if any(a.lower() in u.get("name", "").lower() for a in getattr(p, "aliases", []) or [p.key.lower()]):
                    edges.append({"from": f"proj:{p.key}", "to": f"url:{u['name']}"})

        return {"center": {"id": "center", "label": "Brain", "type": "center"}, "nodes": nodes, "edges": edges}

    def _brain_stream(self) -> None:
        from urllib.parse import parse_qs, urlparse
        try:
            qs = parse_qs(urlparse(self.path).query)
            q = (qs.get("question") or [""])[0].strip()
            if not q:
                self._send(400, b"missing question")
                return
            provider = (qs.get("provider") or [None])[0]
            from . import brain
            # Build context (retrieval) BEFORE sending headers
            project_key = brain._detect_project(q)
            docs = brain._retrieve(q, k=8, project_key=project_key)
            from .web_ask import guard_web_docs  # browser PR 7: web clips are untrusted
            docs, web_rules = guard_web_docs(docs)
            context, citations = brain._format_context(docs)
            system = brain.SYSTEM_PROMPT_BASE.format(today=time.strftime("%Y-%m-%d"), preferences="(none yet)")
            system, lessons_used = brain.apply_lessons(system, q, project_key)  # L4
            system += "\n\nRelevant context (cite with [N]):\n" + (context or "(no matches)") + web_rules
            messages = [{"role": "user", "content": q}]
            provider_obj = brain.resolve_provider(provider)
        except _pe.ProviderUnavailableError as e:
            _pe.send(self, e); return
        except Exception as _e:
            import sys as _sys
            import traceback as _tb
            _sys.stderr.write(f"[_brain_stream setup] EXC: {type(_e).__name__}: {_e}\n")
            _tb.print_exc(file=_sys.stderr)
            _sys.stderr.flush()
            try:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(str(_e).encode())))
                self.end_headers()
                self.wfile.write(str(_e).encode())
            except Exception:
                pass
            return
        # Send SSE headers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        import json as _json
        meta = {
            "type": "meta",
            "citations": citations,
            "provider": provider_obj.name,
            "model": getattr(provider_obj, "model", None),
            "project_key": project_key,
            "lessons_used": lessons_used,
        }
        try:
            self.wfile.write(b"data: " + _json.dumps(meta).encode() + b"\n\n")
            self.wfile.flush()
        except Exception:
            return
        # Stream the LLM (Fix #1: if provider fails AFTER meta is sent, we still
        # emit a structured error event so the client shows a useful message)
        try:
            full = []
            _stream_iter = provider_obj.stream(system, messages)
        except Exception as _stream_setup_err:
            from . import brain as _brain_err
            _perr = _brain_err._translate_provider_error(_stream_setup_err, provider_obj.name)
            try:
                self.wfile.write(b"data: " + _json.dumps({
                    "type": "error",
                    "code": _perr.code,
                    "error": _perr.message,
                    "recoverable": _perr.code in ("rate_limited", "provider_down", "unreachable"),
                }, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
            except Exception:
                pass
            return
        try:
            for chunk in _stream_iter:
                full.append(chunk)
                evt = {"type": "delta", "content": chunk}
                self.wfile.write(b"data: " + _json.dumps(evt, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: " + _json.dumps({"type": "done"}).encode() + b"\n\n")
            self.wfile.flush()
            brain.mark_lessons_used(lessons_used, "".join(full))  # L4
            # Persist (best-effort, atomic via save_conversation_turn — audit 2026-06-23)
            try:
                conv_id = db.save_conversation_turn(
                    title=q[:80], user_content=q,
                    assistant_content="".join(full),
                    citations=citations, provider=meta["provider"], model=meta["model"],
                )
            except Exception:
                conv_id = None  # best-effort; SSE response already sent
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.wfile.write(b"data: " + _json.dumps({"type": "error", "message": str(e)}).encode() + b"\n\n")
            except Exception:
                pass

    def _task_stream(self, path: str) -> None:
        """Server-Sent Events stream of a task run.

        Replays the latest finished run, or waits for the current run
        (or starts a new one) and streams events until 'done'.
        """
        import json as _json
        import threading as _threading
        m = re.match(r"^/api/tasks/(\d+)/stream$", path)
        if not m:
            self._send(404, b"not found"); return
        task_id = int(m.group(1))
        task = db.get_task(task_id)
        if not task:
            self._send(404, b'{"error":"task not found"}', "application/json"); return

        # If the task has a finished run, replay it
        runs = db.list_runs(task_id, limit=1)
        if runs and runs[0]["status"] in ("done", "error", "needs_approval"):
            r = runs[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                self.wfile.write(b"data: " + _json.dumps({
                    "type": "start",
                    "run_id": r["id"],
                    "provider": r["provider"],
                    "model": r["model"],
                    "replay": True,
                }).encode() + b"\n\n")
                tcs = []
                try: tcs = _json.loads(r.get("tool_calls") or "[]")
                except Exception: tcs = []
                for t in tcs:
                    self.wfile.write(b"data: " + _json.dumps({
                        "type": "tool_call",
                        "name": t.get("name"),
                        "args": t.get("args"),
                        "ok": t.get("ok"),
                    }).encode() + b"\n\n")
                if r.get("output_text"):
                    self.wfile.write(b"data: " + _json.dumps({
                        "type": "text", "text": r["output_text"],
                    }).encode() + b"\n\n")
                if r.get("artifact_doc_id"):
                    self.wfile.write(b"data: " + _json.dumps({
                        "type": "artifact",
                        "doc_id": r["artifact_doc_id"],
                        "path": r.get("output_path"),
                    }).encode() + b"\n\n")
                self.wfile.write(b"data: " + _json.dumps({
                    "type": "done",
                    "status": r["status"],
                    "duration_ms": r.get("duration_ms", 0),
                    "artifact_doc_id": r.get("artifact_doc_id"),
                    "output_path": r.get("output_path"),
                    "error": r.get("error"),
                }).encode() + b"\n\n")
                self.wfile.flush()
            except Exception: pass
            return

        # Otherwise: start a run and stream
        from . import orchestrator
        # Send SSE headers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        done_event = _threading.Event()
        events_buffer: list[dict] = []
        finish_lock = _threading.Lock()

        def _on_event(event: dict) -> None:
            with finish_lock:
                events_buffer.append(event)
            try:
                self.wfile.write(b"data: " + _json.dumps(event, default=str).encode() + b"\n\n")
                self.wfile.flush()
            except Exception:
                pass
            if event.get("type") == "done":
                done_event.set()

        # Run synchronously in this request thread (BaseHTTPRequestHandler is
        # already threaded, one request per thread). The handler blocks until
        # the LLM finishes, which is fine for local Ollama.
        try:
            orchestrator.run_task(task_id, sync=True, on_event=_on_event)
        except Exception as e:
            try:
                self.wfile.write(b"data: " + _json.dumps({"type": "error", "message": str(e)}).encode() + b"\n\n")
                self.wfile.flush()
            except Exception: pass
        return

    def do_POST(self) -> None:
        """POST handlers."""
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        # Same auth gate as do_PUT/do_DELETE — see security note there.
        # Public POSTs: signup/login. (The Stripe webhook was removed with
        # billing on 2026-10-21 — ConductLoop is free.)
        # Pairing POSTs skip session+CSRF but are hard-gated to localhost inside
        # their handlers (only someone at the machine can pair a new device).
        _PUBLIC_POSTS = {"/api/auth/signup", "/api/auth/login",
                         "/api/pair/start", "/api/pair/revoke",
                         "/api/brain/providers/key", "/api/agents/create", "/api/agents/run"}
        # Cross-site / foreign-Origin writes are refused for every path, public or
        # not, loopback included (origin_guard, 2026-10-21). seed-starter is no
        # longer public: it gets the normal write gate.
        from . import origin_guard as _og  # CORS allowlist + cross-site write guard
        if not _og.guard(self, "POST", path):
            return
        # Local-first trust: loopback requests (the desktop owner at the machine)
        # may mutate without a session. Remote (LAN/relay) still needs a bearer
        # token — _is_local() returns False when X-Forwarded-For/X-Relay is set.
        if path not in _PUBLIC_POSTS and not self._is_local() and not self._current_user():
            self._send(401, b'{"error":"authentication required"}', "application/json"); return
        if path not in _PUBLIC_POSTS and not self._is_local() and not self._check_csrf():
            self._send(403, b'{"error":"csrf token missing or invalid"}', "application/json"); return
        try:
            path = urlparse(self.path).path
            # Route registry (#4/#6): registered POST routes run after the auth
            # gate above, before the legacy chain.
            from . import routes_meta  # noqa: F401  (import registers routes)
            if _router.dispatch(self, "POST", path):
                return
            # ── Per-CEO channel bindings (2026-07-16) ─────────────────────
            if path.startswith("/api/boardroom/ceo/") and path.endswith("/channels") and path.count("/") == 5:
                try: ceo_id = int(path.split("/")[4])
                except ValueError:
                    self._send(400, b'{"error":"bad ceo id"}', "application/json"); return
                try: payload = self._read_json()
                except Exception: payload = {}
                platform = (payload.get("platform") or "").strip()
                target_id = (payload.get("target_id") or "").strip()
                target_name = (payload.get("target_name") or "").strip() or None
                direction = (payload.get("direction") or "both").strip()
                if not platform or not target_id:
                    self._send(400, b'{"error":"platform and target_id required"}', "application/json"); return
                from . import ceo_channels as _cc
                out = _cc.bind_channel(ceo_id, platform, target_id, target_name, direction)
                status = 200 if out.get("ok") else 400
                self._send(status, json.dumps(out, default=str).encode(), "application/json"); return
            if path.startswith("/api/boardroom/ceo/") and path.endswith("/channels/test") and path.count("/") == 6:
                # Send a test message from the CEO to all its bound outbound channels.
                try: ceo_id = int(path.split("/")[4])
                except ValueError:
                    self._send(400, b'{"error":"bad ceo id"}', "application/json"); return
                try: payload = self._read_json()
                except Exception: payload = {}
                text = (payload.get("text") or "👋 test from ConductLoop").strip()
                from . import ceo_channels as _cc
                self._send(200, json.dumps(_cc.publish_outbound(ceo_id, text, kind="test"), default=str).encode(), "application/json"); return
            if path.startswith("/api/boardroom/ceo/") and path.endswith("/channels/inbound") and path.count("/") == 6:
                try: ceo_id = int(path.split("/")[4])
                except ValueError:
                    self._send(400, b'{"error":"bad ceo id"}', "application/json"); return
                try: payload = self._read_json()
                except Exception: payload = {}
                platform = (payload.get("platform") or "").strip()
                target_id = str(payload.get("target_id") or "")
                text = (payload.get("text") or "").strip()
                author = (payload.get("author") or "").strip() or None
                if not platform or not target_id or not text:
                    self._send(400, b'{"error":"platform, target_id, text required"}', "application/json"); return
                from . import boardroom as _br_post
                body = f"[{author}] {text}" if author else text
                msg = _br_post.append_ceo_message(ceo_id, None, "chairman", body)
                self._send(200, json.dumps({"ok": True, "appended": msg}, default=str).encode(), "application/json"); return
            if path.startswith("/api/boardroom/ceo/") and path.count("/") == 6:
                # /api/boardroom/ceo/<id>/channels/<channel_id>
                # POST = toggle, DELETE = unbind
                parts = path.split("/")
                try:
                    ceo_id = int(parts[4])
                    channel_id = int(parts[6])
                except ValueError:
                    self._send(400, b'{"error":"bad ceo id or channel id"}', "application/json"); return
                from . import ceo_channels as _cc
                if self.command == "DELETE":
                    out = _cc.unbind_channel(channel_id, ceo_id=ceo_id)
                else:
                    try: payload = self._read_json()
                    except Exception: payload = {}
                    out = _cc.toggle_channel(channel_id, bool(payload.get("enabled", True)), ceo_id=ceo_id)
                status = 200 if out.get("ok") else 400
                self._send(status, json.dumps(out).encode(), "application/json"); return
            if path.startswith("/api/boardroom/ceo/") and path.endswith("/channels/test"):
                # Send a test message from the CEO to all its bound outbound channels.
                try: ceo_id = int(path.split("/")[4])
                except ValueError:
                    self._send(400, b'{"error":"bad ceo id"}', "application/json"); return
                try: payload = self._read_json()
                except Exception: payload = {}
                text = (payload.get("text") or "👋 test from ConductLoop").strip()
                from . import ceo_channels as _cc
                self._send(200, json.dumps(_cc.publish_outbound(ceo_id, text, kind="test"), default=str).encode(), "application/json"); return
            if path.startswith("/api/boardroom/ceo/") and path.endswith("/channels/inbound"):
                # A message arrived from a bound channel. Route it as a chairman
                # message to the CEO thread.
                try: ceo_id = int(path.split("/")[4])
                except ValueError:
                    self._send(400, b'{"error":"bad ceo id"}', "application/json"); return
                try: payload = self._read_json()
                except Exception: payload = {}
                platform = (payload.get("platform") or "").strip()
                target_id = str(payload.get("target_id") or "")
                text = (payload.get("text") or "").strip()
                author = (payload.get("author") or "").strip() or None
                if not platform or not target_id or not text:
                    self._send(400, b'{"error":"platform, target_id, text required"}', "application/json"); return
                # Manual inbound for a single CEO (gateway or test).
                # We append a chairman message directly; the full inbound router
                # (multi-CEO) is in route_inbound(platform, target_id, ...).
                from . import boardroom as _br_post
                from . import ceo_channels as _cc
                body = f"[{author}] {text}" if author else text
                msg = _br_post.append_ceo_message(ceo_id, None, "chairman", body)
                self._send(200, json.dumps({"ok": True, "appended": msg}, default=str).encode(), "application/json"); return
            if path == "/api/boardroom/channels/route-inbound":
                # Multi-CEO inbound router (called by Telegram/Discord webhooks).
                try: payload = self._read_json()
                except Exception: payload = {}
                platform = (payload.get("platform") or "").strip()
                target_id = str(payload.get("target_id") or "")
                text = (payload.get("text") or "").strip()
                author = (payload.get("author") or "").strip() or None
                if not platform or not target_id or not text:
                    self._send(400, b'{"error":"platform, target_id, text required"}', "application/json"); return
                from . import ceo_channels as _cc
                out = _cc.route_inbound(platform, target_id, text, author)
                self._send(200, json.dumps(out, default=str).encode(), "application/json"); return
            if path == "/api/boardroom/channels/stats":
                from . import ceo_channels as _cc
                self._send(200, json.dumps(_cc.stats()).encode(), "application/json"); return
            if path == "/api/boardroom/seed-starter":
                # Idempotent seed of the curated CEO starter pack.
                # Pass {"force": true} to wipe non-Holding CEOs first.
                try: payload = self._read_json()
                except Exception: payload = {}
                force = bool(payload.get("force"))
                if force and payload.get("confirm") != "delete":
                    # force wipes every non-Holding CEO/VP/director: demand an
                    # explicit confirmation token, not just a boolean.
                    self._send(400, json.dumps({
                        "ok": False, "error": "confirmation_required", "code": "confirmation_required",
                        "message": 'force deletes existing AI CEOs; send {"force": true, "confirm": "delete"}',
                    }).encode(), "application/json"); return
                from . import db as _db
                from .seed_agents import seed_starter_pack
                with _db.get_connection() as conn:
                    if force:
                        conn.execute("""
                            DELETE FROM team_members WHERE type='ai_agent'
                            AND level IN ('ceo','vp','director')
                            AND id NOT IN (SELECT id FROM team_members WHERE manager_id IS NULL)
                        """)
                    summary = seed_starter_pack(
                        conn,
                        chairman_user_id=(self._current_user() or {}).get("id", 0) or 0,
                    )
                    conn.commit()
                self._send(200, json.dumps(summary, default=str).encode(), "application/json"); return
            if path == "/api/pair/start":
                self._pair_start(); return
            if path == "/api/pair/revoke":
                self._pair_revoke(); return
            if path == "/api/brain/providers/key":
                self._provider_set_key(); return
            # ── Boardroom (2026-07-01): POST endpoints
            from . import boardroom as _boardroom_post
            if path == "/api/boardroom/create":
                self._boardroom_create()
                return
            if path.startswith("/api/boardroom/ceo/") and path.endswith("/chat"):
                self._boardroom_chat()
                return
            if path == "/api/agents/create":
                self._agent_create(); return
            if path == "/api/agents/run":
                self._agent_run(); return
            if path.startswith("/api/agents/"):
                from urllib.parse import parse_qs, urlparse
                tail = path[len("/api/agents/"):]
                parts = tail.split("/")
                try: mid = int(parts[0])
                except (ValueError, IndexError):
                    self._send(400, b'{"error":"bad id"}', "application/json"); return
                if len(parts) == 2 and parts[1] == "memory":
                    self._agent_memory_handler(mid); return
                if len(parts) == 2 and parts[1] == "triggers":
                    self._agent_trigger_handler(mid); return
                if len(parts) == 2 and parts[1] == "wake":
                    self._agent_wake_handler(mid); return
                if len(parts) == 2 and parts[1] == "tools":
                    self._agent_tools(mid); return
                if len(parts) == 3 and parts[1] == "memory":
                    try: mem_id = int(parts[2])
                    except ValueError: self._send(400, b'{"error":"bad memory id"}', "application/json"); return
                    self._agent_memory_action(mid, mem_id); return
                if len(parts) == 3 and parts[1] == "triggers":
                    try: tid = int(parts[2])
                    except ValueError: self._send(400, b'{"error":"bad trigger id"}', "application/json"); return
                    self._agent_trigger_action(mid, tid); return
                self._send(404, b'{"error":"unknown agent subpath"}', "application/json"); return
            if path.startswith("/api/approvals/"):
                m = re.match(r"^/api/approvals/(\d+)/(approve|reject)$", path)
                if m and self.command == "POST":
                    self._approval_action(int(m.group(1)))
                    return
                self._send(404, b'{"error":"not found"}', "application/json"); return
            if path == "/api/brain/ask":
                self._brain_ask()
                return
            if path == "/api/brain/ask-tools":
                self._brain_ask_tools()
                return
            if path == "/api/boardroom/recap/refresh":
                # Force-regenerate Holding CEO synthesis (POST alias of the GET route).
                payload = _boardroom_post._recap_payload()
                synthesis = _boardroom_post.recap_synthesis(payload, force=True)
                self._send(200, json.dumps({"synthesis": synthesis, "regenerated_at": int(__import__('time').time())}, default=str).encode(), "application/json")
                return
            # ── Telegram bridge (2026-07-01): POST webhook delivery + set/clear webhook ──
            if path == "/api/telegram/webhook":
                from . import telegram_bridge as _tg_post
                secret_header = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
                if not _tg_post._authorized(secret_header):
                    self._send(403, b'{"error":"unauthorized"}', "application/json"); return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = self.rfile.read(length).decode("utf-8") if length else ""
                    update = json.loads(body) if body else {}
                except Exception as e:
                    self._send(400, json.dumps({"error": f"bad json: {e}"}).encode(), "application/json"); return
                try:
                    result = _tg_post.handle_update(update)
                    self._send(200, json.dumps(result, default=str).encode(), "application/json")
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            if path == "/api/telegram/chats-bind":
                from . import telegram_bridge as _tg_post
                try: payload = self._read_json()
                except Exception: payload = {}
                chat_id = payload.get("chat_id")
                try: chat_id_int = int(chat_id) if chat_id is not None else None
                except Exception: chat_id_int = None
                if chat_id_int is None:
                    self._send(400, b'{"error":"chat_id required"}', "application/json"); return
                user = self._current_user()
                user_id = (user.get("id") if user else None) or payload.get("user_id") or _boardroom_post.current_chairman_id()
                out = _tg_post.bind_chat(chat_id_int, user_id)
                self._send(200, json.dumps(out, default=str).encode(), "application/json")
                return
            if path == "/api/telegram/set_webhook":
                from . import telegram_bridge as _tg_post
                try: payload = self._read_json()
                except Exception: payload = {}
                public_url = (
                    (payload.get("url") or "").strip()
                    or os.environ.get("NEXT_PUBLIC_SITE_URL", "").strip()
                    or os.environ.get("OPS_PUBLIC_URL", "").strip()
                    or f"http://localhost:{os.environ.get('OPS_PORT', '7777')}"
                )
                secret = (payload.get("secret") or _tg_post.WEBHOOK_SECRET or None)
                out = _tg_post.set_webhook(public_url, secret=secret)
                self._send(200, json.dumps({"ok": out.get("ok"), "result": out, "public_url": public_url}, default=str).encode(), "application/json")
                return
            # ── Discord bridge POST endpoints (2026-07-01) ────────────────────
            if path == "/api/slack/test_send":
                from . import slack_bridge as _sl_post
                try: payload = self._read_json()
                except Exception: payload = {}
                target_id = (payload.get("channel_id") or payload.get("target_id") or "").strip()
                text = (payload.get("text") or "👋 test from ConductLoop").strip()
                if not target_id:
                    self._send(400, b'{"error":"channel_id required"}', "application/json"); return
                self._send(200, json.dumps(_sl_post.send_message(target_id, text), default=str).encode(), "application/json"); return
            if path == "/api/slack/webhook":
                # Slack Events API webhook. Public POST (Slack calls this without auth).
                from urllib.parse import urlparse as _up

                from . import slack_bridge as _sl_wh
                _qs = _up(self.path).query
                secret = None
                if _qs:
                    from urllib.parse import parse_qs
                    secret = (parse_qs(_qs).get("secret") or [None])[0]
                # Fail closed: every request is rejected unless SLACK_SIGNING_SECRET
                # is configured and X-Slack-Signature verifies over the raw body.
                try:
                    _len = int(self.headers.get("Content-Length", "0") or 0)
                except ValueError:
                    _len = -1
                if _len < 0 or _len > 1024 * 1024:
                    self._send(413, b'{"error":"body too large"}', "application/json"); return
                raw = self.rfile.read(_len) if _len else b""
                ok, reason = _sl_wh.authorize_request(
                    body=raw, timestamp=self.headers.get("X-Slack-Request-Timestamp"),
                    signature=self.headers.get("X-Slack-Signature"), query_secret=secret)
                if not ok:
                    status = 503 if reason == "signing_secret_not_configured" else 401
                    self._send(status, json.dumps({"error": reason}).encode(), "application/json"); return
                try: payload = json.loads(raw.decode("utf-8") or "{}")
                except Exception: payload = {}
                if isinstance(payload, dict) and payload.get("type") == "url_verification":
                    self._send(200, json.dumps({"challenge": str(payload.get("challenge") or "")}).encode(),
                               "application/json"); return
                out = _sl_wh.handle_event(payload if isinstance(payload, dict) else {})
                self._send(200, json.dumps(out, default=str).encode(), "application/json"); return
            if path == "/api/discord/test_send":
                from . import discord_bridge as _dc_post
                try: payload = self._read_json()
                except Exception: payload = {}
                content = (payload.get("content") or "👋 Holding CEO bridge online (test).").strip()
                username = (payload.get("username") or "ConductLoop Holding CEO").strip()
                out = _dc_post.send_message(content, username=username)
                self._send(200, json.dumps(out, default=str).encode(), "application/json")
                return
            if path == "/api/discord/webhook":
                from . import discord_bridge as _dc_post
                token_header = self.headers.get("X-Discord-Token")
                if not _dc_post._authorized(token_header):
                    self._send(403, b'{"error":"unauthorized"}', "application/json"); return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = self.rfile.read(length).decode("utf-8") if length else ""
                    update = json.loads(body) if body else {}
                except Exception as e:
                    self._send(400, json.dumps({"error": f"bad json: {e}"}).encode(), "application/json"); return
                try:
                    result = _dc_post.handle_update(update)
                    self._send(200, json.dumps(result, default=str).encode(), "application/json")
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            if path == "/api/discord/broadcast":
                from . import discord_bridge as _dc_post
                try: payload = self._read_json()
                except Exception: payload = {}
                content = (payload.get("content") or "").strip()
                if not content:
                    self._send(400, b'{"error":"content required"}', "application/json"); return
                out = _dc_post.send_message(content)
                self._send(200, json.dumps(out, default=str).encode(), "application/json")
                return
            if path == "/api/telegram/clear_webhook":
                from . import telegram_bridge as _tg_post
                out = _tg_post.delete_webhook()
                self._send(200, json.dumps({"ok": out.get("ok"), "result": out}, default=str).encode(), "application/json")
                return
            if path == "/api/brain/memory":
                self._brain_memory_set()
                return
            if path == "/api/code_todos/done":
                from . import db as _db
                try:
                    payload = self._read_json()
                    _db.mark_code_todo_done(int(payload["id"]), done=bool(payload.get("done", True)))
                    self._send(200, b'{"ok":true}', "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            if path == "/api/revenue/sources":
                self._revenue_sources(); return
            if path == "/api/revenue/seed-mock":
                self._revenue_seed_mock(); return
            if path == "/api/backlinks/scan":
                from .indexers import backlinks
                doc_id = None
                try: payload = self._read_json()
                except Exception: payload = {}
                if payload.get("doc_id"):
                    try: doc_id = int(payload["doc_id"])
                    except Exception: pass
                result = backlinks.run(doc_id=doc_id)
                self._send(200, json.dumps(result).encode(), "application/json"); return
            if re.match(r"^/api/notes/\d+/ai-action$", path) and self.command == "POST":
                try: doc_id = int(path.split("/")[3])
                except ValueError: self._send(400, b'{"error":"bad id"}', "application/json"); return
                self._notes_ai_action(doc_id); return
            if re.match(r"^/api/notes/\d+/save$", path) and self.command == "POST":
                try: doc_id = int(path.split("/")[3])
                except ValueError: self._send(400, b'{"error":"bad id"}', "application/json"); return
                self._notes_save(doc_id); return
            if path == "/api/sentry/projects":
                self._sentry_projects(); return
            if path == "/api/sentry/seed-mock":
                self._sentry_seed_mock(); return
            if path.startswith("/api/sentry/projects/"):
                tail = path[len("/api/sentry/projects/"):]
                parts = tail.split("/")
                try: sid = int(parts[0])
                except (ValueError, IndexError):
                    self._send(400, b'{"error":"bad id"}', "application/json"); return
                if len(parts) == 2 and parts[1] == "test":
                    self._sentry_project_test(sid); return
                if len(parts) == 2 and parts[1] == "poll":
                    self._sentry_project_poll(sid); return
                self._send(404, b'{"error":"unknown subpath"}', "application/json"); return
            if path == "/api/revenue/margin":
                self._revenue_margin(); return
            if path == "/api/cost/seed-mock":
                self._cost_seed_mock(); return
            if path == "/api/digest":
                self._digest_api(); return
            if path == "/api/auth/signup":
                self._auth_signup(); return
            if path == "/api/auth/login":
                self._auth_login(); return
            if path == "/api/auth/logout":
                self._auth_logout(); return
            if path in _BILLING_GONE:
                self._billing_gone(); return
            if path == "/api/ga/sites":
                self._ga_sites(); return
            if path == "/api/ga/seed-mock":
                self._ga_seed_mock(); return
            if path.startswith("/api/ga/sites/"):
                tail = path[len("/api/ga/sites/"):]
                parts = tail.split("/")
                try: sid = int(parts[0])
                except (ValueError, IndexError):
                    self._send(400, b'{"error":"bad id"}', "application/json"); return
                if len(parts) == 2 and parts[1] == "test":
                    self._ga_site_test(sid); return
                if len(parts) == 2 and parts[1] == "poll":
                    self._ga_site_poll(sid); return
                self._send(404, b'{"error":"unknown subpath"}', "application/json"); return
                return
            if path == "/api/connections":
                self._connections(); return
            if path.startswith("/api/connections/"):
                tail = path[len("/api/connections/"):]
                parts = tail.split("/")
                try: cid = int(parts[0])
                except (ValueError, IndexError):
                    self._send(400, b'{"error":"bad id"}', "application/json"); return
                if len(parts) == 2 and parts[1] == "test":
                    self._connection_test(cid); return
                self._send(404, b'{"error":"unknown subpath"}', "application/json"); return
            if path.startswith("/api/revenue/sources/"):
                tail = path[len("/api/revenue/sources/"):]
                parts = tail.split("/")
                try: sid = int(parts[0])
                except (ValueError, IndexError):
                    self._send(400, b'{"error":"bad id"}', "application/json"); return
                if len(parts) == 2 and parts[1] == "test":
                    self._revenue_source_test(sid); return
                if len(parts) == 2 and parts[1] == "poll":
                    self._revenue_source_poll(sid); return
                self._send(404, b'{"error":"unknown subpath"}', "application/json"); return
            if path == "/api/mcp/servers":
                self._mcp_servers(); return
            if path.startswith("/api/mcp/servers/") and path.endswith("/discover"):
                name = path[len("/api/mcp/servers/"):-len("/discover")].rstrip("/")
                self._mcp_discover(name); return
            if path == "/api/decision":
                from . import db as _db
                try:
                    payload = self._read_json()
                    did = _db.create_decision(
                        project_key=payload.get("project_key"),
                        title=payload["title"],
                        context=payload.get("context", ""),
                        decision=payload["decision"],
                        consequences=payload.get("consequences"),
                        links=payload.get("links"),
                        tags=payload.get("tags"),
                    )
                    self._send(200, json.dumps({"id": did, "ok": True}).encode(), "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            if path == "/api/cost":
                from . import db as _db
                try:
                    payload = self._read_json()
                    cents = int(payload.get("amount_cents") or round(float(payload["amount"]) * 100))
                    cid = _db.add_cost(
                        provider=payload["provider"],
                        amount_cents=cents,
                        currency=payload.get("currency", "EUR"),
                        project_key=payload.get("project_key"),
                        category=payload.get("category", "infra"),
                        description=payload.get("description"),
                        period=payload.get("period"),
                    )
                    self._send(200, json.dumps({"id": cid, "ok": True}).encode(), "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            if path.startswith("/api/templates/") and path.endswith("/instantiate"):
                from . import db as _db
                try:
                    tail = path[len("/api/templates/"):-len("/instantiate")]
                    try:
                        payload = self._read_json()
                    except Exception:
                        payload = {}
                    if tail.startswith("by-key/"):
                        key = tail[len("by-key/"):]
                        ids = _db.instantiate_template(key, project_key=payload.get("project_key"))
                    else:
                        # by id - find key first
                        try: tid = int(tail)
                        except ValueError:
                            self._send(400, b'{"error":"bad id"}', "application/json"); return
                        rows = _db.list_templates()
                        tpl = next((t for t in rows if t["id"] == tid), None)
                        if not tpl:
                            self._send(404, b'{"error":"not found"}', "application/json"); return
                        ids = _db.instantiate_template(tpl["key"], project_key=payload.get("project_key"))
                    self._send(200, json.dumps({"ok": True, "ids": ids}).encode(), "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            if path == "/api/github/sync":
                from . import db as _db
                from .indexers import github as gh
                try:
                    counters = gh.run()
                    self._send(200, json.dumps({"ok": True, "counters": counters,
                                                "summary": _db.github_summary()}).encode(), "application/json")
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            if path == "/api/voice/transcribe":
                self._voice_transcribe()
                return
            if path == "/api/tasks":
                # Create a task + (optionally) trigger a run
                data = self._read_json()
                title = (data.get("title") or "").strip()
                prompt = (data.get("prompt") or "").strip()
                if not title or not prompt:
                    self._send(400, b'{"error":"title and prompt are required"}', "application/json")
                    return
                task_id = db.create_task(
                    project_key=(data.get("project_key") or "").strip() or None,
                    title=title,
                    prompt=prompt,
                    agent_member_id=data.get("agent_member_id"),
                    provider=data.get("provider"),
                    model=data.get("model"),
                    created_by=data.get("created_by") or "ui",
                )
                # Trigger a run by default (sync=False so the API responds fast)
                run_result = None
                if data.get("auto_run", True):
                    from . import orchestrator
                    run_result = orchestrator.run_task(task_id, sync=False)
                resp = {"ok": True, "id": task_id, "task": db.get_task(task_id)}
                if run_result:
                    resp["run"] = run_result
                self._send(200, _json(resp).encode(), "application/json")
                return
            m = re.match(r"^/api/members/(\d+)/chat$", path)
            if m:
                self._member_chat(int(m.group(1)))
                return
            if re.match(r"^/api/members/\d+/reports$", path):
                self._add_report(path)
                return
            # POST /api/tasks/<id>/run
            m = re.match(r"^/api/tasks/(\d+)/run$", path)
            if m:
                from . import orchestrator
                tid = int(m.group(1))
                try: data = self._read_json()
                except Exception: data = {}
                sync = bool(data.get("sync", False))
                result = orchestrator.run_task(tid, sync=sync)
                _pe.send_result(self, result, 200 if result.get("ok") else 400, _json(result).encode())
                return
            # POST /api/tasks/<id>/approve  (the <id> is a task_id, we resolve to latest run)
            m = re.match(r"^/api/tasks/(\d+)/approve$", path)
            if m:
                from . import orchestrator
                task_id = int(m.group(1))
                result = orchestrator.approve_run(task_id=task_id)
                status_code = 200 if result.get("ok") else 400
                self._send(status_code, _json(result).encode(), "application/json")
                return
            if path == "/api/tags":
                self._tags_action()
                return
            if path == "/api/user_todos":
                self._user_todos_create()
                return
            if path.startswith("/api/user_todos/"):
                self._user_todo_delete(int(path[len("/api/user_todos/"):]))
                return
            if path == "/api/ideas":
                self._ideas_create()
                return
            if path == "/api/docs/create":
                self._docs_create()
                return
            if path == "/api/teams":
                self._teams_create()
                return
            if path.startswith("/api/teams/"):
                self._team_action(path[len("/api/teams/"):])
                return
            if path == "/api/members":
                self._members_create()
                return
            if path.startswith("/api/members/"):
                self._member_action(path[len("/api/members/"):])
                return
            if path == "/api/teams":
                self._teams_create()
                return
            if path.startswith("/api/teams/"):
                self._team_action(path[len("/api/teams/"):])
                return
            if path == "/api/members":
                self._members_create()
                return
            if path.startswith("/api/members/"):
                self._member_action(path[len("/api/members/"):])
                return
            if path == "/api/teams":
                self._teams_create()
                return
            if path.startswith("/api/teams/"):
                self._team_action(path[len("/api/teams/"):])
                return
            if path == "/api/members":
                self._members_create()
                return
            if path.startswith("/api/members/"):
                self._member_action(path[len("/api/members/"):])
                return
            if path.startswith("/api/ideas/"):
                self._idea_delete(int(path[len("/api/ideas/"):]))
                return
            # /api/playbook/<key>/entry
            if not path.startswith("/api/playbook/"):
                self._send(404, b"not found")
                return
            parts = path.split("/")
            if len(parts) != 5 or parts[4] != "entry":
                self._send(404, b"not found")
                return
            key = parts[3]
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                data = json.loads(body)
            except Exception:
                self._send(400, b'{"error":"bad json"}', "application/json")
                return
            content = (data.get("content") or "").strip()
            if not content:
                self._send(400, b'{"error":"content required"}', "application/json")
                return
            kind = data.get("kind") or "agent_in_use"
            done = bool(data.get("done", False))
            self._add_entry(key, kind, content, done)
            self._send(200, b'{"ok":true}', "application/json")
        except Exception as e:
            self._send_500(e)

    def do_PUT(self) -> None:
        """PUT /api/playbook/<key>/entry/<id>  PUT /api/user_todos/<id>  PUT /api/ideas/<id>  PUT /api/decision/<id>  PUT /api/members/<id>  PUT /api/teams/<id>  PUT /api/agents/<id>/memory/<id>  PUT /api/agents/<id>/triggers/<id>  PUT /api/connections/<id>  PUT /api/mcp/servers/<name>"""
        from urllib.parse import urlparse

        # CRITICAL: every state-changing request must be authenticated.
        # Audit 2026-06-23 found unauthenticated PUT could rewrite any
        # team_members row (IDOR). Reject anonymous mutations globally.
        from . import origin_guard as _og  # CORS allowlist + cross-site write guard
        if not _og.guard(self, "PUT", urlparse(self.path).path):
            return
        if not self._is_local() and not self._current_user():
            self._send(401, b'{"error":"authentication required"}', "application/json"); return
        # CSRF check (double-submit cookie) — skipped for loopback (local owner).
        if not self._is_local() and not self._check_csrf():
            self._send(403, b'{"error":"csrf token missing or invalid"}', "application/json"); return
        try:
            path = urlparse(self.path).path
            if path == "/api/docs/save":
                self._docs_save(); return
            if path == "/api/docs/rename":
                self._docs_rename(); return
            if path.startswith("/api/connections/"):
                try: cid = int(path[len("/api/connections/"):].split("/")[0])
                except ValueError: self._send(400, b'{"error":"bad id"}', "application/json"); return
                try: payload = self._read_json()
                except Exception: payload = {}
                ok = db.update_connection(cid, **payload)
                if not ok: self._send(400, b'{"error":"bad update"}', "application/json"); return
                self._send(200, b'{"ok":true}', "application/json"); return
            if path.startswith("/api/mcp/servers/") and path.count("/") == 4:
                name = path[len("/api/mcp/servers/"):]
                try: payload = self._read_json()
                except Exception: payload = {}
                # Update is done via the create+overwrite path
                command = payload.get("command") or []
                if isinstance(command, str): command = command.split()
                db.upsert_mcp_server(name, payload.get("transport", "stdio"),
                                      command=command, url=payload.get("url"),
                                      env=payload.get("env"))
                self._send(200, b'{"ok":true}', "application/json"); return
            if path.startswith("/api/agents/"):
                tail = path[len("/api/agents/"):]
                parts = tail.split("/")
                try: mid = int(parts[0])
                except (ValueError, IndexError):
                    self._send(400, b'{"error":"bad id"}', "application/json"); return
                if len(parts) == 3 and parts[1] == "memory":
                    try: mem_id = int(parts[2])
                    except ValueError: self._send(400, b'{"error":"bad memory id"}', "application/json"); return
                    self._agent_memory_action(mid, mem_id); return
                if len(parts) == 3 and parts[1] == "triggers":
                    try: tid = int(parts[2])
                    except ValueError: self._send(400, b'{"error":"bad trigger id"}', "application/json"); return
                    self._agent_trigger_action(mid, tid); return
                self._send(404, b'{"error":"unknown subpath"}', "application/json"); return
            if path.startswith("/api/user_todos/"):
                self._user_todo_action(path[len("/api/user_todos/"):])
                return
            if path.startswith("/api/ideas/"):
                self._idea_action(path[len("/api/ideas/"):])
                return
            # ── Boardroom (2026-07-01): PUT autonomy
            if path.startswith("/api/boardroom/autonomy/"):
                self._boardroom_set_autonomy()
                return
            if path.startswith("/api/members/"):
                try:
                    mid = int(path[len("/api/members/"):])
                    payload = self._read_json()
                    ok = db.update_member(mid, **payload)
                    if not ok: self._send(404, b'{"error":"not found"}', "application/json"); return
                    self._send(200, b'{"ok":true}', "application/json")
                except ValueError:
                    self._send(400, b'{"error":"bad id"}', "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            if path.startswith("/api/teams/"):
                try:
                    tid = int(path[len("/api/teams/"):])
                    payload = self._read_json()
                    ok = db.update_team(tid, **payload)
                    if not ok: self._send(404, b'{"error":"not found"}', "application/json"); return
                    self._send(200, b'{"ok":true}', "application/json")
                except ValueError:
                    self._send(400, b'{"error":"bad id"}', "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            if path.startswith("/api/connections/"):
                try:
                    cid = int(path[len("/api/connections/"):].split("/")[0])
                    ok = db.delete_connection(cid)
                    if not ok: self._send(404, b'{"error":"not found"}', "application/json"); return
                    self._send(200, b'{"ok":true}', "application/json")
                except ValueError:
                    self._send(400, b'{"error":"bad id"}', "application/json")
                return
            if path.startswith("/api/mcp/servers/"):
                name = path[len("/api/mcp/servers/"):]
                ok = db.delete_mcp_server(name)
                if not ok: self._send(404, b'{"error":"not found"}', "application/json"); return
                self._send(200, b'{"ok":true}', "application/json")
                return
            if path == "/api/docs/delete":
                self._docs_delete()
                return
            if path.startswith("/api/decision/"):
                from . import db as _db
                try:
                    did = int(path[len("/api/decision/"):])
                    payload = self._read_json()
                    ok = _db.update_decision(did, **payload)
                    self._send(200, json.dumps({"ok": ok}).encode(), "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            if not path.startswith("/api/playbook/"):
                self._send(404, b"not found")
                return
            parts = path.split("/")
            if len(parts) != 6 or parts[4] != "entry":
                self._send(404, b"not found")
                return
            key, entry_id = parts[3], int(parts[5])
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                data = json.loads(body)
            except Exception:
                self._send(400, b'{"error":"bad json"}', "application/json")
                return
            self._update_entry(key, entry_id, data)
            self._send(200, b'{"ok":true}', "application/json")
        except Exception as e:
            self._send_500(e)

    def _orgchart(self) -> dict:
        """Return the agent org chart: projects → teams → members + cross-links.

        Layout:
          {
            stats: {total_projects, total_teams, total_members, humans, ai, systems, active, paused, avg_workload},
            projects: [
              {
                key, name, color,
                teams: [
                  {id, name, key, type, icon, member_count, members: [...]},
                ],
              },
            ],
            orphans: { teams: [...], members: [...] },  // teams/members not in any project
          }
        """
        from . import projects as proj_registry
        # Index all members by team
        all_teams = db.list_teams()
        teams_by_id = {t["id"]: t for t in all_teams}
        members_by_team: dict[int, list[dict]] = {}
        for t in all_teams:
            members_by_team[t["id"]] = db.list_members(team_id=t["id"])

        # Map each team to a project via team.key (teams seeded as ev-trading-labs, rentcla, etc.)
        # If a team key matches a project, attach; otherwise it goes to "orphans".
        proj_by_key = {p.key: p for p in proj_registry.PROJECTS}
        teams_per_project: dict[str, list[dict]] = {}
        orphan_teams: list[dict] = []
        for t in all_teams:
            enriched = dict(t)
            enriched["members"] = members_by_team.get(t["id"], [])
            proj = proj_by_key.get(t["key"])
            if proj:
                teams_per_project.setdefault(proj.key, []).append(enriched)
            else:
                orphan_teams.append(enriched)

        # Build the projects list
        projects_out = []
        for p in proj_registry.PROJECTS:
            teams = teams_per_project.get(p.key, [])
            member_count = sum(len(t["members"]) for t in teams)
            projects_out.append({
                "key": p.key,
                "name": p.name,
                "color": getattr(p, "color", "cyan"),
                "team_count": len(teams),
                "member_count": member_count,
                "teams": teams,
            })

        # Stats
        all_members = [m for ms in members_by_team.values() for m in ms]
        humans    = [m for m in all_members if m["type"] == "human"]
        ai_agents = [m for m in all_members if m["type"] == "ai_agent"]
        systems   = [m for m in all_members if m["type"] == "system"]
        active    = [m for m in all_members if m.get("status") == "active"]
        paused    = [m for m in all_members if m.get("status") == "paused"]
        workloads = [m.get("workload") or 0 for m in all_members]
        avg_wl    = sum(workloads) / len(workloads) if workloads else 0
        max_wl    = max(workloads) if workloads else 0
        return {
            "stats": {
                "total_projects": len(proj_registry.PROJECTS),
                "total_teams": len(all_teams),
                "total_members": len(all_members),
                "humans": len(humans),
                "ai_agents": len(ai_agents),
                "systems": len(systems),
                "active": len(active),
                "paused": len(paused),
                "avg_workload": round(avg_wl, 1),
                "max_workload": max_wl,
            },
            "projects": projects_out,
            "orphans": {"teams": orphan_teams, "members": []},
        }

    def _brain_ask_tools(self) -> None:
        from . import brain
        from . import tools as brain_tools
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(body)
        except Exception:
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        q = (data.get("question") or "").strip()
        if not q:
            self._send(400, b'{"error":"question required"}', "application/json")
            return
        provider_name = data.get("provider")
        try:
            provider = brain.resolve_provider(provider_name)
            if not provider.supports_tools():
                self._send(400, b'{"error":"provider does not support tools"}', "application/json")
                return
            project_key = brain._detect_project(q)
            today = time.strftime("%Y-%m-%d")
            prefs = db.memory_list()
            pref_str = "\n".join(f"- {p['key']}: {p['value']}" for p in prefs) or "(none yet)"
            system = brain.SYSTEM_PROMPT_BASE.format(today=today, preferences=pref_str)
            messages = [{"role": "user", "content": q}]
            tool_log = []
            def on_call(name, args, result):
                tool_log.append({"name": name, "args": args, "ok": result.get("ok", False)})
            prov_tools = brain_tools.tools_for_anthropic() if provider.name == "anthropic" else brain_tools.tools_for_openai()
            try:
                answer = provider.ask_with_tools(system, messages, prov_tools, on_tool_call=on_call)
            except Exception as e:
                perr = brain._translate_provider_error(e, provider.name)
                self._send(perr.status or 502, _json({
                    "error": perr.message, "code": perr.code,
                    "provider": provider.name, "recoverable": perr.code in ("rate_limited","provider_down","unreachable"),
                }, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            conv_id = db.save_conversation_turn(
                title=q[:80], user_content=q, assistant_content=answer,
                provider=provider.name, model=getattr(provider, "model", None),
            )
            self._send(200, _json({
                "conversation_id": conv_id,
                "answer": answer,
                "provider": provider.name,
                "model": getattr(provider, "model", None),
                "project_key": project_key,
                "tool_log": tool_log,
            }).encode(), "application/json")
        except _pe.ProviderUnavailableError as e:
            _pe.send(self, e)
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def do_DELETE(self) -> None:
        """DELETE /api/user_todos/<id>  DELETE /api/ideas/<id>  DELETE /api/decision/<id>  DELETE /api/cost/<id>  DELETE /api/members/<id>  DELETE /api/teams/<id>  DELETE /api/agents/<id>/memory/<id>  DELETE /api/agents/<id>/triggers/<id>  DELETE /api/agents/<id>/tools  DELETE /api/connections/<id>  DELETE /api/mcp/servers/<name>"""
        from urllib.parse import urlparse

        # Same auth gate as do_PUT — loopback (local owner) bypasses; remote needs bearer.
        from . import origin_guard as _og  # CORS allowlist + cross-site write guard
        if not _og.guard(self, "DELETE", urlparse(self.path).path):
            return
        if not self._is_local() and not self._current_user():
            self._send(401, b'{"error":"authentication required"}', "application/json"); return
        if not self._is_local() and not self._check_csrf():
            self._send(403, b'{"error":"csrf token missing or invalid"}', "application/json"); return
        try:
            path = urlparse(self.path).path
            # Per-CEO channel bindings (2026-07-16): DELETE /api/boardroom/ceo/<id>/channels/<channel_id>
            if path.startswith("/api/boardroom/ceo/") and path.count("/") == 6 and self.command == "DELETE":
                parts = path.split("/")
                try:
                    ceo_id = int(parts[4]); channel_id = int(parts[6])
                except ValueError:
                    self._send(400, b'{"error":"bad ceo id or channel id"}', "application/json"); return
                from . import ceo_channels as _cc_del
                out = _cc_del.unbind_channel(channel_id, ceo_id=ceo_id)
                status = 200 if out.get("ok") else 404
                self._send(status, json.dumps(out).encode(), "application/json"); return
            if path.startswith("/api/agents/"):
                tail = path[len("/api/agents/"):]
                parts = tail.split("/")
                try: mid = int(parts[0])
                except (ValueError, IndexError):
                    self._send(400, b'{"error":"bad id"}', "application/json"); return
                if len(parts) == 3 and parts[1] == "memory":
                    try: mem_id = int(parts[2])
                    except ValueError: self._send(400, b'{"error":"bad memory id"}', "application/json"); return
                    self._agent_memory_action(mid, mem_id); return
                if len(parts) == 3 and parts[1] == "triggers":
                    try: tid = int(parts[2])
                    except ValueError: self._send(400, b'{"error":"bad trigger id"}', "application/json"); return
                    self._agent_trigger_action(mid, tid); return
                if len(parts) == 2 and parts[1] == "tools":
                    self._agent_tool_unbind(mid); return
                self._send(404, b'{"error":"unknown subpath"}', "application/json"); return
            if path.startswith("/api/connections/"):
                try:
                    cid = int(path[len("/api/connections/"):].split("/")[0])
                    self._connection_action(cid); return
                except ValueError:
                    self._send(400, b'{"error":"bad id"}', "application/json"); return
            if path.startswith("/api/mcp/servers/"):
                name = path[len("/api/mcp/servers/"):].split("/")[0]
                if not name:
                    self._send(400, b'{"error":"bad name"}', "application/json"); return
                self._mcp_server_action(name); return
            if path.startswith("/api/revenue/sources/"):
                tail = path[len("/api/revenue/sources/"):].split("/")
                try: sid = int(tail[0])
                except (ValueError, IndexError):
                    self._send(400, b'{"error":"bad id"}', "application/json"); return
                self._revenue_source_action(sid); return
            if path.startswith("/api/ga/sites/"):
                tail = path[len("/api/ga/sites/"):].split("/")
                try: sid = int(tail[0])
                except (ValueError, IndexError):
                    self._send(400, b'{"error":"bad id"}', "application/json"); return
                self._ga_site_action(sid); return
            if path.startswith("/api/sentry/projects/"):
                tail = path[len("/api/sentry/projects/"):].split("/")
                try: sid = int(tail[0])
                except (ValueError, IndexError):
                    self._send(400, b'{"error":"bad id"}', "application/json"); return
                self._sentry_project_action(sid); return
            if path.startswith("/api/user_todos/"):
                self._user_todo_delete(int(path[len("/api/user_todos/"):]))
                return
            if path.startswith("/api/ideas/"):
                self._idea_delete(int(path[len("/api/ideas/"):]))
                return
            if path.startswith("/api/members/"):
                try:
                    mid = int(path[len("/api/members/"):])
                    ok = db.delete_member(mid)
                    if not ok: self._send(404, b'{"error":"not found"}', "application/json"); return
                    self._send(200, b'{"ok":true}', "application/json")
                except ValueError:
                    self._send(400, b'{"error":"bad id"}', "application/json")
                return
            if path.startswith("/api/teams/"):
                try:
                    tid = int(path[len("/api/teams/"):])
                    ok = db.delete_team(tid)
                    if not ok: self._send(404, b'{"error":"not found"}', "application/json"); return
                    self._send(200, b'{"ok":true}', "application/json")
                except ValueError:
                    self._send(400, b'{"error":"bad id"}', "application/json")
                return
            if path.startswith("/api/decision/"):
                from . import db as _db
                try:
                    did = int(path[len("/api/decision/"):])
                    ok = _db.delete_decision(did)
                    self._send(200, json.dumps({"ok": ok}).encode(), "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            if path.startswith("/api/cost/"):
                from . import db as _db
                try:
                    cid = int(path[len("/api/cost/"):])
                    ok = _db.delete_cost(cid)
                    self._send(200, json.dumps({"ok": ok}).encode(), "application/json")
                except Exception as e:
                    self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            self._send(404, b'{"error":"not found"}', "application/json")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def _brain_memory_set(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(body)
        except Exception:
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        key = (data.get("key") or "").strip()
        value = (data.get("value") or "").strip()
        if not key or not value:
            self._send(400, b'{"error":"key and value required"}', "application/json")
            return
        db.memory_set(key, value)
        self._send(200, b'{"ok":true}', "application/json")

    def _brain_ask(self) -> None:
        from . import brain
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(body)
        except Exception:
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        q = (data.get("question") or "").strip()
        if not q:
            self._send(400, b'{"error":"question required"}', "application/json")
            return
        conv_id = data.get("conversation_id")
        provider = data.get("provider")
        try:
            ans = brain.ask(q, conversation_id=conv_id, provider_name=provider)
        except _pe.ProviderUnavailableError as e:
            _pe.send(self, e); return
        except brain.ProviderError as e:
            # User-actionable provider failure — friendly message + stable code (Fix #1)
            self._send(
                e.status or 502,
                json.dumps({
                    "error": e.message,
                    "code": e.code,
                    "provider": provider or "auto",
                    "recoverable": e.code in ("rate_limited", "provider_down", "unreachable"),
                }, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )
            return
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return
        if not conv_id:
            conv_id = db.save_conversation_turn(
                title=q[:80], user_content=q, assistant_content=ans.answer,
                citations=ans.citations, provider=ans.provider, model=ans.model,
            )
        self._send(200, _json({
            "conversation_id": conv_id,
            "answer": ans.answer,
            "citations": ans.citations,
            "provider": ans.provider,
            "model": ans.model,
            "project_key": ans.project_key,
            "duration_ms": ans.duration_ms,
            "memory_proposals": ans.memory_proposals,
        }).encode(), "application/json")

    def _tags_action(self) -> None:
        """POST /api/tags  body: {doc_id, tag, action: 'add'|'remove'}"""
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(body)
        except Exception:
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        doc_id = data.get("doc_id")
        tag = (data.get("tag") or "").strip()
        action = data.get("action", "add")
        if not doc_id or not tag:
            self._send(400, b'{"error":"doc_id and tag required"}', "application/json")
            return
        if action == "remove":
            db.remove_tag(int(doc_id), tag)
        else:
            db.add_tag(int(doc_id), tag)
        self._send(200, _json({"ok": True, "tags": db.tags_for_doc(int(doc_id))}).encode(), "application/json")

    # ---- docs (files) handlers ----
    # These power the manual editing of indexed documents from the Files view.
    # Auth gate is the global do_POST/do_PUT/do_DELETE one — see notes there.

    def _docs_create(self) -> None:
        """POST /api/docs/create  body: {source, path, title, content}
        Creates a new indexed document. Returns {ok, id}."""
        try:
            data = self._read_json()
        except Exception:
            self._send(400, b'{"error":"bad json"}', "application/json"); return
        source = (data.get("source") or "").strip()
        path = (data.get("path") or "").strip()
        title = (data.get("title") or "").strip() or None
        content = data.get("content") or ""
        if not source or not path:
            self._send(400, b'{"error":"source and path required"}', "application/json"); return
        try:
            new_id = db.create_document(source=source, path=path, title=title, content=content)
        except Exception as e:
            self._send(400, _json({"error": str(e)}).encode(), "application/json"); return
        self._send(200, _json({"ok": True, "id": new_id}).encode(), "application/json")

    def _docs_save(self) -> None:
        """PUT /api/docs/save  body: {doc_id, content}
        Updates an existing document's content. Returns {ok, mtime}."""
        try:
            data = self._read_json()
        except Exception:
            self._send(400, b'{"error":"bad json"}', "application/json"); return
        doc_id = data.get("doc_id")
        content = data.get("content") or ""
        if not doc_id:
            self._send(400, b'{"error":"doc_id required"}', "application/json"); return
        ok = db.update_document_content(int(doc_id), content)
        if not ok:
            self._send(404, b'{"error":"not found"}', "application/json"); return
        # Touch indexed_at via rename trick would be cleaner, but update_document_content
        # already sets indexed_at to now(), so the next indexer run will skip this file
        # (mtime unchanged). That's fine: re-running the indexer will reload content
        # from disk and re-upsert.
        self._send(200, _json({"ok": True}).encode(), "application/json")

    def _docs_rename(self) -> None:
        """PUT /api/docs/rename  body: {doc_id, title?, path?, source?}
        Updates title and/or path (move between projects by changing source).
        Returns {ok}."""
        try:
            data = self._read_json()
        except Exception:
            self._send(400, b'{"error":"bad json"}', "application/json"); return
        doc_id = data.get("doc_id")
        if not doc_id:
            self._send(400, b'{"error":"doc_id required"}', "application/json"); return
        ok = db.rename_document(
            int(doc_id),
            title=data.get("title"),
            path=data.get("path"),
        )
        if not ok:
            self._send(404, b'{"error":"not found or no change"}', "application/json"); return
        self._send(200, _json({"ok": True}).encode(), "application/json")

    def _docs_delete(self) -> None:
        """DELETE /api/docs/delete  body: {doc_id}  (in query string)
        Removes the document from the index. The original file on disk is NOT
        touched — only the indexed row. This is intentional: index = cache."""
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try:
            doc_id = int((qs.get("doc_id") or ["0"])[0])
        except Exception:
            self._send(400, b'{"error":"bad doc_id"}', "application/json"); return
        ok = db.delete_document(doc_id)
        if not ok:
            self._send(404, b'{"error":"not found"}', "application/json"); return
        self._send(200, _json({"ok": True}).encode(), "application/json")

    # ---- user_todos handlers ----

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            return json.loads(body)
        except Exception:
            return {}

    # ============================================================
    # Auth + Billing
    # ============================================================

    def _session_token(self) -> str | None:
        """Extract session token from Cookie header (format: ops_session=<token>)."""
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("ops_session="):
                return part[len("ops_session="):]
        return None

    def _bearer_token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):].strip() or None
        return None

    def _current_user(self) -> dict | None:
        """Resolve the caller from (a) a paired-device Bearer token, or
        (b) a session cookie. Returns a user dict, a synthetic device dict, or None."""
        # (a) Mobile / API client: paired device bearer token
        raw = self._bearer_token()
        if raw:
            dev = db.get_device_token(raw)
            if dev:
                try: db.touch_device_token(raw)
                except Exception: pass
                uid = dev.get("user_id")
                if uid:
                    u = db.get_user(uid)
                    if u: return u
                # Single-user local install: no user row — return a device principal
                return {"id": None, "device": True, "name": dev.get("name") or "device"}
            return None
        # (b) Browser session cookie
        tok = self._session_token()
        if not tok:
            return None
        sess = db.get_session(tok)
        if not sess:
            return None
        return db.get_user(sess["user_id"])

    def parse_request(self) -> bool:
        """Stdlib hook run before every do_* method (also OPTIONS): reject
        loopback requests whose Host is not this server (DNS rebinding; see
        host_guard), then, inside the desktop app, require the per-launch app
        cookie on browser-engine loopback requests (see app_token_guard)."""
        if not super().parse_request():
            return False
        from . import app_token_guard, host_guard
        return host_guard.check(self) and app_token_guard.check(self)

    def _is_local(self) -> bool:
        """True if the request comes from the same machine (loopback).

        NOTE: when a relay is added (step 2), the relay must forward an
        X-Forwarded-For/Origin so we DON'T treat relayed traffic as local."""
        if self.headers.get("X-Forwarded-For") or self.headers.get("X-Relay"):
            return False
        ip = self.client_address[0] if self.client_address else ""
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost")

    def _lan_ips(self) -> list[str]:
        """Best-effort LAN IPv4 addresses of this machine (excludes loopback)."""
        import socket
        ips: set[str] = set()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except Exception:
            pass
        return sorted(ip for ip in ips if ip and not ip.startswith("127."))

    def _tailscale_ips(self) -> list[str]:
        """Tailscale IPv4(s) of this machine, for remote access from outside the
        LAN. Empty if Tailscale isn't installed/running. Tailscale uses the
        100.64.0.0/10 CGNAT range."""
        import re
        import shutil
        import subprocess
        for exe in (shutil.which("tailscale"),
                    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                    "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale"):
            if not exe:
                continue
            try:
                out = subprocess.run([exe, "ip", "-4"], capture_output=True,
                                     text=True, timeout=4)
                ips = [l.strip() for l in out.stdout.splitlines() if l.strip()]
                if ips:
                    return ips
            except Exception:
                continue
        # Fallback: scan interfaces for the CGNAT range Tailscale assigns.
        try:
            out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=4).stdout
            return re.findall(r"inet (100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+)", out)
        except Exception:
            return []

    def _pair_start(self) -> None:
        """POST /api/pair/start — mint a paired-device token. LOCAL ONLY.

        Body (optional): {"name": "iPhone"}. Returns the raw token ONCE plus a
        QR payload + candidate LAN URLs the phone can connect to."""
        if not self._is_local():
            self._send(403, b'{"error":"pairing must be started on the host machine"}', "application/json"); return
        try:
            data = self._read_json()
        except Exception:
            data = {}
        name = (data.get("name") or "device").strip()[:60]
        # Attach to the single owner user if exactly one exists (single-tenant local).
        user_id = None
        try:
            u = self._current_user()
            if u and u.get("id"):
                user_id = u["id"]
        except Exception:
            pass
        raw = db.create_device_token(name=name, user_id=user_id)
        port = self.server.server_address[1] if getattr(self, "server", None) else 7777
        # LAN first (fast at home), then Tailscale (works remotely). The phone
        # tries each and uses the first reachable.
        lan = [f"http://{ip}:{port}" for ip in self._lan_ips()]
        ts = [f"http://{ip}:{port}" for ip in self._tailscale_ips()]
        hosts = lan + [h for h in ts if h not in lan]
        payload = json.dumps({"v": 1, "name": name, "token": raw, "hosts": hosts},
                             separators=(",", ":"))
        self._send(200, _json({
            "ok": True,
            "name": name,
            "token": raw,           # shown ONCE; only the hash is stored
            "hosts": hosts,
            "qr_payload": payload,
        }).encode(), "application/json")

    def _agent_run(self) -> None:
        """POST /api/agents/run — run an agent on demand. LOCAL ONLY.
        Body: {id, prompt?}. Wakes the agent via the scheduler (uses the
        configured provider + tools) and logs the run."""
        if not self._is_local():
            self._send(403, b'{"error":"local only"}', "application/json"); return
        try:
            data = self._read_json()
            aid = int(data["id"])
        except Exception:
            self._send(400, b'{"error":"id required"}', "application/json"); return
        prompt = (data.get("prompt") or "").strip() or "(manual run)"
        try:
            from . import scheduler as sched_mod
            r = sched_mod._wake_agent(aid, prompt)
        except Exception as e:
            self._send(500, _json({"ok": False, "error": str(e)}).encode(), "application/json"); return
        _pe.send_result(self, r, 200 if r.get("ok") else 500, _json(r).encode())

    # ── Boardroom (2026-07-01) handlers ─────────────────────────────────

    def _boardroom_ceo_stream(self, ceo_id: int, from_id: int = 0) -> None:
        """Server-Sent Events for live CEO replies.
        Polls ceo_messages every 1s; emits a data event with the new row payload
        when role != 'chairman' (i.e. ceo / system). Sends a heartbeat ping every
        15s so proxies don't time out the connection. Closes on 90s idle or when
        the client disconnects."""
        import time as _time
        with db.get_connection() as conn:
            row = conn.execute("SELECT id, name FROM team_members WHERE id = ? AND is_ceo = 1", (ceo_id,)).fetchone()
        if not row:
            self._send(404, b'{"error":"ceo not found"}', "application/json"); return
        # Write SSE response headers
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # nginx hint
            self.end_headers()
        except Exception:
            return
        last_id = from_id
        last_ping = _time.time()
        start = _time.time()
        conn = db.get_connection()
        try:
            while True:
                if _time.time() - start > 600:
                    # Hard cap 10 min to avoid zombie connections
                    break
                # Poll for new messages
                rows = conn.execute(
                    "SELECT id, role, content, created_at, user_id FROM ceo_messages "
                    "WHERE member_id = ? AND id > ? AND role != 'chairman' "
                    "ORDER BY id ASC LIMIT 5",
                    (ceo_id, last_id),
                ).fetchall()
                for r in rows:
                    last_id = max(last_id, int(r["id"]))
                    payload = json.dumps(dict(r), default=str)
                    msg = f"event: message\ndata: {payload}\n\n"
                    try: self.wfile.write(msg.encode("utf-8"))
                    except Exception: return
                    try: self.wfile.flush()
                    except Exception: return
                # Heartbeat
                if _time.time() - last_ping > 15:
                    try:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    except Exception: return
                    last_ping = _time.time()
                # Cooperative shutdown check (peer closed)
                if hasattr(self, "wfile") and self.wfile.closed:
                    return
                _time.sleep(1.0)
        finally:
            try: conn.close()
            except Exception: pass

    def _boardroom_create(self) -> None:
        """POST /api/boardroom/create — Wizard submission (Identity → Tools → Autonomy).
        Body: {name, project_slug, persona_summary, tier, maturity, tools[]}.
        Returns the new CEO row (with id). Persists the audit decision."""
        from . import boardroom as _br
        user = self._current_user()
        if not self._is_local():
            ok, err = _br.require_chairman(user)
            if not ok:
                self._send(403, json.dumps({"error": err}).encode(), "application/json"); return
        try:
            payload = self._read_json()
        except Exception:
            payload = {}
        name = (payload.get("name") or "").strip()
        if not name:
            self._send(400, json.dumps({"error": "name required"}).encode(), "application/json"); return
        try:
            ceo = _br.create_ceo(
                name=name,
                project_slug=(payload.get("project_slug") or "").strip() or None,
                persona_summary=(payload.get("persona_summary") or "").strip(),
                tier=int(payload.get("tier") or 2),
                maturity=payload.get("maturity") or "traction",
                tools=list(payload.get("tools") or []),
                chairman_user_id=user["id"] if user else None,
            )
            self._send(200, json.dumps({"ok": True, "ceo": ceo}, default=str).encode(), "application/json")
        except ValueError as e:
            self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def _boardroom_chat(self) -> None:
        """POST /api/boardroom/ceo/<id>/chat — Chairman sends a message to a CEO thread.
        Body: {content}. Returns 202 immediately with the persisted chairman row;
        spawns a background LLM call that will write the CEO reply under role='ceo'.
        The thread view (or inbox SSE) should poll to surface the response."""
        from urllib.parse import urlparse

        from . import boardroom as _br
        path = urlparse(self.path).path
        try: ceo_id = int(path.split("/")[4])
        except (ValueError, IndexError):
            self._send(400, b'{"error":"bad ceo id"}', "application/json"); return
        try:
            payload = self._read_json()
        except Exception:
            payload = {}
        content = (payload.get("content") or "").strip()
        if not content:
            self._send(400, b'{"error":"content required"}', "application/json"); return
        user = self._current_user()
        # Verify the CEO exists before writing (don't leak 500 instead of 404).
        with db.get_connection() as conn:
            row = conn.execute("SELECT id FROM team_members WHERE id = ? AND is_ceo = 1", (ceo_id,)).fetchone()
        if not row:
            self._send(404, json.dumps({"error": "ceo not found"}).encode(), "application/json"); return
        try:
            msg = _br.append_ceo_message(ceo_id, user["id"] if user else None, "chairman", content)
            # Spawn background LLM call so the CEO auto-replies.
            _br.spawn_ceo_reply(ceo_id, msg["id"])
            # 202 Accepted: chairman row persisted, CEO reply coming async.
            self._send(202, json.dumps({
                "ok": True,
                "message": msg,
                "status": "thinking",
                "note": "ceo reply is being generated in the background — refresh the thread in a few seconds",
            }, default=str).encode(), "application/json")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def _boardroom_set_autonomy(self) -> None:
        """PUT /api/boardroom/autonomy/<id> — Save autonomy config for a CEO.
        Body: {default_tier, maturity_stage, per_action, require_chairman}.
        Persists audit decision row."""
        import json as _json
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        try: ceo_id = int(path.split("/")[4])
        except (ValueError, IndexError):
            self._send(400, b'{"error":"bad ceo id"}', "application/json"); return
        try:
            payload = self._read_json()
        except Exception:
            payload = {}
        user = self._current_user()
        try:
            tier = int(payload.get("default_tier") or 2)
            maturity = payload.get("maturity_stage") or "traction"
            per_action = payload.get("per_action")
            if not isinstance(per_action, str):
                per_action = _json.dumps(per_action or {})
            require_chairman = int(payload.get("require_chairman") or 0)
            now = int(__import__("time").time())
            with db.get_connection() as conn:
                exists = conn.execute(
                    "SELECT name FROM team_members WHERE id = ? AND is_ceo = 1", (ceo_id,)
                ).fetchone()
                if not exists:
                    self._send(404, json.dumps({"error": "ceo not found"}).encode(), "application/json"); return
                cur = conn.execute(
                    "SELECT 1 FROM autonomy_config WHERE member_id = ?", (ceo_id,)
                ).fetchone()
                if cur:
                    conn.execute(
                        "UPDATE autonomy_config SET default_tier=?, maturity_stage=?, per_action=?, require_chairman=?, updated_at=? "
                        "WHERE member_id = ?",
                        (tier, maturity, per_action, require_chairman, now, ceo_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO autonomy_config (member_id, default_tier, maturity_stage, per_action, require_chairman, updated_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (ceo_id, tier, maturity, per_action, require_chairman, now),
                    )
                ceo_name = conn.execute("SELECT name FROM team_members WHERE id=?", (ceo_id,)).fetchone()
                ceo_name_str = ceo_name["name"] if ceo_name else f"#{ceo_id}"
                # Fall back to the configured chairman when no session — keeps the
                # audit log populated in local dev too.
                audit_uid = (user.get("id") if (user and user.get("id")) else None)
                if audit_uid is None:
                    settings_row = conn.execute(
                        "SELECT chairman_user_id FROM boardroom_settings WHERE id = 1"
                    ).fetchone()
                    audit_uid = settings_row["chairman_user_id"] if settings_row else None
                if audit_uid:
                    conn.execute(
                        "INSERT INTO boardroom_decisions (user_id, member_id, kind, title, body, created_at) "
                        "VALUES (?, ?, 'autonomy_changed', ?, ?, ?)",
                        (
                            audit_uid, ceo_id,
                            f"Autonomy updated: {ceo_name_str}",
                            _json.dumps({"tier": tier, "maturity": maturity, "per_action_count": len(_json.loads(per_action) or {})}),
                            now,
                        ),
                    )
                conn.commit()
            self._send(200, json.dumps({"ok": True, "member_id": ceo_id}).encode(), "application/json")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def _agent_create(self) -> None:
        """POST /api/agents/create — create an AI agent. LOCAL ONLY.
        Body: {name, role?, model?, capabilities?, team_id?, trigger_cron?, prompt?}.
        Defaults the team to the built-in AI Squad. Optionally adds a cron trigger."""
        if not self._is_local():
            self._send(403, b'{"error":"local only"}', "application/json"); return
        try:
            data = self._read_json()
        except Exception:
            data = {}
        name = (data.get("name") or "").strip()
        if not name:
            self._send(400, b'{"error":"name required"}', "application/json"); return
        # Resolve team: explicit id, else the built-in AI Squad (seed it if needed).
        team_id = data.get("team_id")
        if not team_id:
            db.seed_default_teams()
            squad = next((t for t in db.list_teams() if t.get("key") == "ai-squad"), None)
            if not squad:
                teams = db.list_teams()
                squad = teams[0] if teams else None
            if not squad:
                self._send(400, b'{"error":"no team available"}', "application/json"); return
            team_id = squad["id"]
        try:
            new_id = db.add_member(
                team_id=int(team_id), name=name, type="ai_agent",
                role=(data.get("role") or "agent"), status="active",
                capabilities=data.get("capabilities"),
                model=(data.get("model") or "minimax"),
                notes=data.get("notes"), level="agent",
            )
        except Exception as e:
            self._send(500, _json({"error": str(e)}).encode(), "application/json"); return
        # Optional: attach a cron trigger so the scheduler wakes it.
        cron = (data.get("trigger_cron") or "").strip()
        prompt = (data.get("prompt") or "").strip()
        if cron and prompt and hasattr(db, "add_trigger"):
            try:
                db.add_trigger(new_id, "cron", cron, prompt=prompt)
            except Exception:
                pass
        self._send(200, _json({"ok": True, "id": new_id}).encode(), "application/json")

    def _provider_set_key(self) -> None:
        """POST /api/brain/providers/key — BYOK: store a provider API key. LOCAL ONLY.
        Body: {provider, api_key?, model?, base_url?, make_default?}."""
        if not self._is_local():
            self._send(403, b'{"error":"local only"}', "application/json"); return
        try:
            data = self._read_json()
        except Exception:
            data = {}
        from . import brain
        try:
            status = brain.set_provider_key(
                data.get("provider", ""),
                api_key=data.get("api_key"),
                model=data.get("model"),
                base_url=data.get("base_url"),
                make_default=bool(data.get("make_default")),
            )
            self._send(200, _json({"ok": True, "providers": status}).encode(), "application/json")
        except ValueError as e:
            self._send(400, _json({"error": str(e)}).encode(), "application/json")
        except Exception as e:
            self._send(500, _json({"error": str(e)}).encode(), "application/json")

    def _pair_revoke(self) -> None:
        """POST /api/pair/revoke — revoke a paired device. LOCAL ONLY. Body: {"id": N}."""
        if not self._is_local():
            self._send(403, b'{"error":"local only"}', "application/json"); return
        try:
            data = self._read_json()
            tid = int(data["id"])
        except Exception:
            self._send(400, b'{"error":"missing id"}', "application/json"); return
        ok = db.revoke_device_token(tid)
        self._send(200 if ok else 404, _json({"ok": ok}).encode(), "application/json")

    def _set_session_cookie(self, token: str, *, max_age_days: int = 30) -> None:
        """Add Set-Cookie headers to the next response. Call BEFORE self._send.

        Audit 2026-06-23: also issue a CSRF token (double-submit cookie pattern).
        The CSRF cookie is HttpOnly=False so JS can read it and send it back in
        X-CSRF-Token header. The session cookie remains HttpOnly for XSS protection.

        Uses _pending_cookies list (not dict) so multiple Set-Cookie headers
        can coexist in one response — previously both cookies collapsed onto
        the same key in _extra_headers and only the last survived.
        """
        max_age = max_age_days * 86400
        if not hasattr(self, "_pending_cookies"):
            self._pending_cookies = []
        # Session cookie (HttpOnly, not readable by JS)
        self._pending_cookies.append(
            f"ops_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
        )
        # CSRF token (NOT HttpOnly, so JS can echo it back; tied to session in-memory)
        csrf = secrets.token_urlsafe(24)
        _CSRF_TOKENS[token] = csrf
        self._pending_cookies.append(
            f"ops_csrf={csrf}; Path=/; SameSite=Lax; Max-Age={max_age}"
        )

    def _check_csrf(self) -> bool:
        """Validate CSRF token for state-changing requests.

        Double-submit cookie pattern: client must send the same token both as
        a cookie (ops_csrf, set by us, readable by JS) and as a request header
        (X-CSRF-Token). If they match AND the token is bound to the current
        session, the request is allowed. This protects against CSRF attacks
        because an attacker on a different origin can't read our cookie value.

        Exceptions (no CSRF needed):
          - GET / HEAD / OPTIONS (read-only or preflight)
          - requests authenticated by Authorization header (API clients)
        """
        # Audit 2026-06-23 fix: was only checking for POST, which meant PUT
        # and DELETE requests always passed CSRF (security hole).
        if self.command in ("GET", "HEAD", "OPTIONS"):
            return True  # read-only / preflight; CSRF not needed
        # Allow Authorization header (Bearer tokens for API clients)
        if self.headers.get("Authorization", "").startswith("Bearer "):
            return True
        cookie_token = self._get_cookie("ops_csrf", "")
        header_token = self.headers.get("X-CSRF-Token", "")
        if not cookie_token or not header_token:
            return False
        if cookie_token != header_token:
            return False
        # Also verify the token is bound to the current session
        session = self._session_token()
        if session:
            bound = _CSRF_TOKENS.get(session)
            if bound != cookie_token:
                return False
        return True

    def _get_cookie(self, name: str, default: str = "") -> str:
        """Extract a single cookie value from the Cookie header."""
        cookie_hdr = self.headers.get("Cookie", "") or ""
        for part in cookie_hdr.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return default

    def _clear_session_cookie(self) -> None:
        if not hasattr(self, "_pending_cookies"):
            self._pending_cookies = []
        # Clear both cookies
        self._pending_cookies.append(
            "ops_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        )
        self._pending_cookies.append(
            "ops_csrf=; Path=/; SameSite=Lax; Max-Age=0"
        )
        # Invalidate any bound CSRF tokens for this session
        session = self._session_token()
        if session:
            _CSRF_TOKENS.pop(session, None)

    def _clear_session_cookie(self) -> None:
        if not hasattr(self, "_extra_headers"):
            self._extra_headers = {}
        self._extra_headers["Set-Cookie"] = (
            "ops_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        )

    def _auth_signup(self) -> None:
        """POST /api/auth/signup — create account, start session."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        try: data = self._read_json()
        except Exception: data = {}
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        name = (data.get("name") or "").strip() or email.split("@")[0]
        locale = data.get("locale") or "en"
        if not email or "@" not in email:
            self._send(400, json.dumps({"error": "invalid email"}).encode(), "application/json"); return
        if len(password) < 8:
            self._send(400, json.dumps({"error": "password too short (min 8 chars)"}).encode(), "application/json"); return
        existing = db.get_user_by_email(email)
        if existing:
            self._send(409, json.dumps({"error": "email already registered"}).encode(), "application/json"); return
        user = db.create_user(email=email, password=password, name=name, locale=locale, email_verified=False)
        if not user:
            self._send(500, json.dumps({"error": "could not create user"}).encode(), "application/json"); return
        # Grant free entitlement by default
        db.grant_entitlement(user_id=user["id"], plan="free", source="signup")
        # Issue session
        token = db.create_session(user["id"], ip=self.client_address[0] if self.client_address else None,
                                  user_agent=(self.headers.get("User-Agent") or "")[:200])
        self._set_session_cookie(token)
        db.update_user(user["id"], last_login_at=int(time.time()))
        db.log_payment_event(user_id=user["id"], kind="signup", payload={"email": email})
        body = json.dumps({"ok": True, "user": {"id": user["id"], "email": user["email"], "name": user["name"], "locale": user["locale"]}}, default=str).encode()
        self._send(200, body, "application/json")

    def _auth_login(self) -> None:
        """POST /api/auth/login — verify password, issue session.

        Audit 2026-06-23: added in-memory rate limiting — max 5 failed
        attempts per IP per 5 minutes (sliding window). After the limit,
        requests are rejected with 429 even for valid credentials to
        slow down brute-force scans. Counter clears on success.
        """
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        # Rate limit check by client IP
        client_ip = (self.client_address[0] if self.client_address else "unknown")
        now = time.time()
        window = _LOGIN_FAILS.get(client_ip, [])
        # Drop entries older than 5 minutes
        window = [t for t in window if now - t < 300]
        if len(window) >= 5:
            self._send(429, json.dumps({"error": "too many login attempts, try again in 5 minutes"}).encode(), "application/json"); return
        try: data = self._read_json()
        except Exception: data = {}
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        if not email or not password:
            self._send(400, json.dumps({"error": "email and password required"}).encode(), "application/json"); return
        user = db.get_user_by_email(email)
        if not user or not db.verify_password(user, password):
            window.append(now)
            _LOGIN_FAILS[client_ip] = window
            self._send(401, json.dumps({"error": "invalid credentials"}).encode(), "application/json"); return
        # Success: clear the failure window for this IP
        _LOGIN_FAILS.pop(client_ip, None)
        token = db.create_session(user["id"], ip=client_ip,
                                  user_agent=(self.headers.get("User-Agent") or "")[:200])
        self._set_session_cookie(token)
        db.update_user(user["id"], last_login_at=int(time.time()))
        db.log_payment_event(user_id=user["id"], kind="login", payload={"email": email})
        body = json.dumps({"ok": True, "user": {"id": user["id"], "email": user["email"], "name": user["name"], "locale": user["locale"]}}, default=str).encode()
        self._send(200, body, "application/json")

    def _auth_logout(self) -> None:
        """POST /api/auth/logout — kill session."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        tok = self._session_token()
        if tok:
            db.delete_session(tok)
        self._clear_session_cookie()
        self._send(200, b'{"ok":true}', "application/json")

    def _auth_me(self) -> None:
        """GET /api/auth/me — current user + entitlement (or null if not logged in)."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        user = self._current_user()
        if not user:
            self._send(200, json.dumps({"user": None, "entitlement": None}).encode(), "application/json"); return
        ent = db.get_entitlement(user["id"])
        body = json.dumps({
            "user": {"id": user["id"], "email": user["email"], "name": user["name"], "locale": user.get("locale") or "en"},
            "entitlement": ent,
            "is_founder": db.is_founder(user["id"]),
            "founder": db.get_founder(user["id"]),
        }, default=str).encode()
        self._send(200, body, "application/json")

    def _billing_plans(self) -> None:
        """GET /api/billing/plans — return available plans and their prices.

        Pivot 2026-07-16: ConductLoop is 100% free. The 'lifetime' plan is
        retired. Founders who paid in the pre-pivot beta (original $300)
        keep their lifetime access — surfaced via the founders roster.
        When cloud-only features ship (cross-device sync, AI hosting,
        team workspaces), per-feature paid tiers will be added here.
        """
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        plans = [
            {
                "id": "free",
                "name": "Free",
                "price_cents": 0,
                "currency": "USD",
                "period": "forever",
                "tagline": "Everything is unlocked. Free, forever.",
                "features": [
                    "All current features",
                    "All future updates",
                    "Community support",
                    "Local-first / single-user",
                ],
                "cta": "Get ConductLoop",
            },
        ]
        # Surface founders roster publicly (no PII, just badge + amount paid)
        founders_public = [
            {"badge": f["badge_color"], "original_cents": f["original_cents"], "joined_at": f["joined_at"]}
            for f in db.list_founders()
        ]
        self._send(200, json.dumps({
            "plans": plans,
            "currency": "USD",
            "founders": founders_public,
            "model": "free_edition",
            "free_edition": True,
            "deprecated_plans": ["lifetime"],
        }, default=str).encode(), "application/json")

    def _billing_gone(self) -> None:
        """Retired billing endpoints (checkout-lifetime, checkout-subscription,
        cancel-subscription, stripe-webhook). ConductLoop is free: the app no
        longer talks to Stripe or holds Stripe keys (2026-10-21, plan R4)."""
        self._send(410, json.dumps({
            "error": "gone",
            "free_edition": True,
            "message": "ConductLoop is free. No checkout, subscription or payment.",
        }).encode(), "application/json")

    def _billing_entitlement(self) -> None:
        """GET /api/billing/entitlement — current user's plan."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        user = self._current_user()
        if not user:
            self._send(200, json.dumps({"plan": "anonymous", "features": []}).encode(), "application/json"); return
        ent = db.get_entitlement(user["id"])
        plan = ent["plan"] if ent else "free"
        # What features are available on each plan
        features_by_plan = {
            "free":                      ["projects", "revenue", "ga", "sentry", "digest"],
            "lifetime":                  ["projects", "revenue", "ga", "sentry", "digest", "ai_agents", "priority_support", "custom_integrations"],
            "pro_monthly":               ["projects", "revenue", "ga", "sentry", "digest", "ai_agents"],
            "team_monthly":              ["projects", "revenue", "ga", "sentry", "digest", "ai_agents", "multi_user", "audit_logs"],
            "small_business_lifetime":   ["projects", "revenue", "ga", "sentry", "digest", "ai_agents", "multi_user", "audit_logs"],
            "team_lifetime":             ["projects", "revenue", "ga", "sentry", "digest", "ai_agents", "priority_support", "custom_integrations", "multi_user", "audit_logs"],
        }
        # Seats: how many users can share this account
        seats_by_plan = {
            "free": 1,
            "lifetime": 1,
            "small_business_lifetime": 3,
            "team_lifetime": 5,
            "pro_monthly": 1,
            "team_monthly": 5,
        }
        features = features_by_plan.get(plan, features_by_plan["free"])
        body = json.dumps({
            "user": {"id": user["id"], "email": user["email"], "name": user["name"], "locale": user.get("locale") or "en"},
            "plan": plan,
            "features": features,
            "seats": seats_by_plan.get(plan, 1),
            "entitlement": ent,
        }, default=str).encode()
        self._send(200, body, "application/json")

    def _voice_transcribe(self) -> None:
        """POST /api/voice/transcribe — multipart form-data or raw audio body.

        Multipart: look for the part named 'audio', use its filename + Content-Type.
        Raw body: use the request Content-Type header.
        """
        from . import voice
        if not voice.is_available():
            self._send(503, json.dumps({
                "error": "faster-whisper not installed",
                "install_hint": "pip install faster-whisper",
            }).encode(), "application/json"); return
        ctype = (self.headers.get("Content-Type") or "").lower()
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            self._send(400, b'{"error":"empty body"}', "application/json"); return
        body = self.rfile.read(length)

        audio_bytes = b""
        suffix = ".webm"
        part_ctype = ctype  # default to top-level for raw bodies

        if ctype.startswith("multipart/form-data"):
            # Parse multipart, extract the 'audio' part. curl and other clients
            # often prepend extra dashes to the boundary delimiter beyond the
            # RFC 7578 standard of exactly two, so normalize before parsing.
            import re
            from email.parser import BytesParser
            from email.policy import default as email_default
            try:
                # Detect the actual boundary from the body (strip leading dashes)
                first_line = body.split(b"\r\n", 1)[0]
                actual_name = first_line.lstrip(b"-").decode("ascii", errors="replace")
                # Normalize: replace any 2+ dashes + name with exactly 2 + name
                normalized_body = re.sub(
                    rb"^-{2,}" + re.escape(actual_name.encode("ascii", errors="replace")),
                    b"--" + actual_name.encode("ascii", errors="replace"),
                    body,
                )
                # Fix Content-Type boundary to match
                normalized_ctype = re.sub(
                    r"boundary=[^;\s]+", f"boundary={actual_name}", ctype
                )
                raw = b"Content-Type: " + normalized_ctype.encode() + b"\r\n\r\n" + normalized_body
                msg = BytesParser(policy=email_default).parsebytes(raw)
                if not msg.is_multipart():
                    raise ValueError("parsed message is not multipart")
                for part in msg.get_payload():
                    if part.get_param("name", header="content-disposition") == "audio":
                        audio_bytes = part.get_payload(decode=True) or b""
                        fn = part.get_filename() or ""
                        if "." in fn:
                            suffix = "." + fn.rsplit(".", 1)[-1].lower()
                        pctype = (part.get_content_type() or "").lower()
                        if pctype and ("wav" in pctype or "ogg" in pctype or "mpeg" in pctype
                                       or "mp4" in pctype or "m4a" in pctype or "flac" in pctype
                                       or "webm" in pctype):
                            part_ctype = pctype
                        break
            except Exception as e:
                self._send(400, json.dumps({"error": f"bad multipart: {e}"}).encode(), "application/json")
                return
            if not audio_bytes:
                self._send(400, b'{"error":"no audio part"}', "application/json"); return
        else:
            audio_bytes = body

        # Pick extension from part/top Content-Type (more reliable than filename)
        if "ogg" in part_ctype: suffix = ".ogg"
        elif "wav" in part_ctype: suffix = ".wav"
        elif "mpeg" in part_ctype or "mp3" in part_ctype: suffix = ".mp3"
        elif "mp4" in part_ctype or "m4a" in part_ctype: suffix = ".m4a"
        elif "flac" in part_ctype: suffix = ".flac"
        elif "webm" in part_ctype: suffix = ".webm"

        try:
            result = voice.transcribe_bytes(audio_bytes, suffix=suffix, save_to_db=True)
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json")
        except Exception as e:
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode("utf-8"), "application/json")

    def _user_todos_list(self) -> None:
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        status = (qs.get("status") or ["open"])[0]
        project = qs.get("project", [None])[0]
        assignee = qs.get("assignee", [None])[0]
        try: limit = int((qs.get("limit") or ["200"])[0])
        except Exception: limit = 200
        if assignee:
            with db.db() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM user_todos WHERE status = ? AND assignee_id = ? ORDER BY priority DESC, created_at DESC LIMIT ?",
                    (status, int(assignee), limit),
                ).fetchall()]
        else:
            rows = db.list_user_todos(status=status, project_key=project, limit=limit)
        self._send(200, _json({"rows": rows, "summary": db.user_todo_summary()}).encode(), "application/json")

    def _user_todo_action(self, tail: str) -> None:
        try:
            todo_id = int(tail)
        except ValueError:
            self._send(400, b'{"error":"bad id"}', "application/json")
            return
        data = self._read_json()
        ok = db.update_user_todo(todo_id, **data)
        if not ok:
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        try: db.recompute_member_workload()
        except Exception: pass
        self._send(200, _json({"ok": True, "id": todo_id}).encode(), "application/json")

    def _user_todos_create(self) -> None:
        data = self._read_json()
        content = (data.get("content") or "").strip()
        if not content:
            self._send(400, b'{"error":"content required"}', "application/json")
            return
        # due_date can be YYYY-MM-DD or unix timestamp
        due = data.get("due_date")
        if due and isinstance(due, str):
            try:
                from datetime import datetime
                due = int(datetime.strptime(due, "%Y-%m-%d").timestamp())
            except Exception: due = None
        new_id = db.add_user_todo(
            content=content,
            project_key=data.get("project_key"),
            tags=data.get("tags") or [],
            priority=(int(data["priority"]) if data.get("priority") is not None and str(data["priority"]).lstrip("-").isdigit() else 0),
            due_date=due,
            assignee_id=data.get("assignee_id"),
        )
        db.add_activity("todo_added", content[:100], data.get("project_key"))
        try: db.recompute_member_workload()
        except Exception: pass
        self._send(200, _json({"ok": True, "id": new_id}).encode(), "application/json")

    def _user_todo_delete(self, todo_id: int) -> None:
        ok = db.delete_user_todo(todo_id)
        if not ok:
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        self._send(200, b'{"ok": true}', "application/json")

    # ---- ideas handlers ----

    def _ideas_list(self) -> None:
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        status = qs.get("status", [None])[0]
        project = qs.get("project", [None])[0]
        try: limit = int((qs.get("limit") or ["200"])[0])
        except Exception: limit = 200
        rows = db.list_ideas(status=status, project_key=project, limit=limit)
        self._send(200, _json({"rows": rows, "summary": db.idea_summary()}).encode(), "application/json")

    def _idea_action(self, tail: str) -> None:
        try:
            idea_id = int(tail)
        except ValueError:
            self._send(400, b'{"error":"bad id"}', "application/json")
            return
        data = self._read_json()
        ok = db.update_idea(idea_id, **data)
        if not ok:
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        self._send(200, _json({"ok": True, "id": idea_id}).encode(), "application/json")

    def _ideas_create(self) -> None:
        data = self._read_json()
        content = (data.get("content") or "").strip()
        if not content:
            self._send(400, b'{"error":"content required"}', "application/json")
            return
        new_id = db.add_idea(
            content=content,
            project_key=data.get("project_key"),
            tags=data.get("tags") or [],
        )
        self._send(200, _json({"ok": True, "id": new_id}).encode(), "application/json")

    def _idea_delete(self, idea_id: int) -> None:
        ok = db.delete_idea(idea_id)
        if not ok:
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        self._send(200, b'{"ok": true}', "application/json")

    def _teams_create(self) -> None:
        data = self._read_json()
        key = (data.get("key") or "").strip()
        name = (data.get("name") or "").strip()
        if not key or not name:
            self._send(400, b'{"error":"key and name required"}', "application/json")
            return
        new_id = db.create_team(
            key=key, name=name, description=data.get("description"),
            type=data.get("type", "mixed"), color=data.get("color"), icon=data.get("icon"),
        )
        self._send(200, _json({"ok": True, "id": new_id}).encode(), "application/json")

    def _team_action(self, tail: str) -> None:
        try: tid = int(tail)
        except ValueError: self._send(400, b'{"error":"bad id"}', "application/json"); return
        if self.command == "PUT":
            data = self._read_json()
            if not db.update_team(tid, **data): self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, b'{"ok": true}', "application/json")
        elif self.command == "DELETE":
            if not db.delete_team(tid): self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, b'{"ok": true}', "application/json")

    def _members_create(self) -> None:
        data = self._read_json()
        team_id = data.get("team_id")
        name = (data.get("name") or "").strip()
        if not team_id or not name:
            self._send(400, b'{"error":"team_id and name required"}', "application/json")
            return
        new_id = db.add_member(
            team_id=int(team_id), name=name, type=data.get("type", "human"),
            role=data.get("role"), status=data.get("status", "active"),
            contact=data.get("contact"), capabilities=data.get("capabilities"),
            model=data.get("model"), notes=data.get("notes"),
        )
        self._send(200, _json({"ok": True, "id": new_id}).encode(), "application/json")

    def _member_action(self, tail: str) -> None:
        try: mid = int(tail)
        except ValueError: self._send(400, b'{"error":"bad id"}', "application/json"); return
        if self.command == "PUT":
            data = self._read_json()
            if not db.update_member(mid, **data): self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, b'{"ok": true}', "application/json")
        elif self.command == "DELETE":
            if not db.delete_member(mid): self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, b'{"ok": true}', "application/json")

    def _member_chat(self, member_id: int) -> None:
        """Chat with a team member (the agent). Uses brain.ask() with a
        member-specific system prompt. Optionally streams via SSE if the
        request has ?stream=1.
        """
        member = db.get_member(member_id) if hasattr(db, "get_member") else None
        if not member:
            # Fallback: get from list_members
            with db.db() as conn:
                row = conn.execute("SELECT * FROM team_members WHERE id = ?", (member_id,)).fetchone()
            if not row:
                self._send(404, b'{"error":"member not found"}', "application/json"); return
            member = dict(row)
        data = self._read_json()
        message = (data.get("message") or "").strip()
        if not message:
            self._send(400, b'{"error":"message is required"}', "application/json"); return
        from . import brain as brain_mod

        # Build the agent's system prompt
        level = member.get("level") or "agent"
        role  = member.get("role")  or "agent"
        name  = member.get("name")  or "Agent"
        caps  = member.get("capabilities") or ""
        model = member.get("model")
        chain = db.get_member_chain(member_id)  # [self, parent, grandparent, ...]
        chain_str = " → ".join(m.get("name") for m in chain) or name

        owner = os.environ.get("OPS_OWNER_NAME", "the operator")
        system_prompt = (
            f"You are {name}, role={role}, level={level} in {owner}'s ops organization.\n"
            f"Reporting chain (self → top): {chain_str}\n"
            f"Capabilities: {caps or '(none specified)'}\n"
            f"Model: {model or 'auto'}\n"
            f"\n"
            f"You are chatting directly with {owner}. Be concise, opinionated, and "
            f"action-oriented. Reference the org structure when relevant ('I'll loop "
            f"in my director' or 'this should go through Marketing'). If the user "
            f"asks you to do something outside your capabilities, say so and suggest "
            f"who in the org should handle it. Today's date: "
            f"{__import__('time').strftime('%Y-%m-%d')}"
        )

        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        stream = (qs.get("stream") or ["0"])[0] in ("1", "true")

        # Persist the conversation + message
        conv_id = data.get("conversation_id")
        if not conv_id:
            conv_id = db.create_conversation(title=f"Chat with {name}")
        db.add_message(conv_id, "user", message)

        # Build messages: include prior history (last 10)
        with db.db() as conn:
            prior = conn.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 20",
                (conv_id,),
            ).fetchall()
        msgs = []
        for m in reversed(prior):
            msgs.append({"role": m["role"], "content": m["content"]})

        provider_name = (qs.get("provider") or [None])[0] or data.get("provider") or None
        if not provider_name and model and ":" in model:
            provider_name = model.split(":", 1)[0]
        try:
            prov = brain_mod.resolve_provider(provider_name)  # before any SSE header
        except _pe.ProviderUnavailableError as e:
            _pe.send(self, e); return
        except Exception as e:
            self._send(500, _json({"ok": False, "error": str(e)}).encode(), "application/json"); return
        try:
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                meta = {"type": "meta", "conversation_id": conv_id, "agent_id": member_id, "agent_name": name}
                self.wfile.write(b"data: " + _json.dumps(meta).encode() + b"\n\n")
                self.wfile.flush()
                # Fallback to mock if requested provider is unavailable (no API key)
                if not prov.available():
                    prov = brain_mod.resolve_provider("mock")
                    self.wfile.write(b"data: " + _json.dumps({"type": "meta", "fallback": True, "requested": provider_name}).encode() + b"\n\n")
                    self.wfile.flush()
                if hasattr(prov, "stream"):
                    full = []
                    try:
                        for chunk in prov.stream(system_prompt, msgs):
                            full.append(chunk)
                            self.wfile.write(b"data: " + _json.dumps({"type": "delta", "text": chunk}).encode() + b"\n\n")
                            self.wfile.flush()
                    except Exception as e:
                        self.wfile.write(b"data: " + _json.dumps({"type": "error", "message": str(e)}).encode() + b"\n\n")
                    text = "".join(full)
                else:
                    text = brain_mod.ask_with_deadline(prov, system_prompt, msgs)
                    self.wfile.write(b"data: " + _json.dumps({"type": "delta", "text": text}).encode() + b"\n\n")
                db.add_message(conv_id, "assistant", text)
                self.wfile.write(b"data: " + _json.dumps({"type": "done", "text": text, "conversation_id": conv_id}).encode() + b"\n\n")
                self.wfile.flush()
            else:
                if not prov.available():
                    prov = brain_mod.resolve_provider("mock")
                text = brain_mod.ask_with_deadline(prov, system_prompt, msgs)
                db.add_message(conv_id, "assistant", text)
                self._send(200, _json({
                    "ok": True, "agent_id": member_id, "conversation_id": conv_id, "text": text,
                }).encode(), "application/json")
        except Exception as e:
            self._send(500, _json({"ok": False, "error": str(e)}).encode(), "application/json")

        # ============================================================
    # Agent memory / triggers / logs
    # ============================================================

    def _agent_memory_handler(self, member_id: int) -> None:
        """GET /api/agents/<id>/memory  POST /api/agents/<id>/memory"""
        if self.command == "GET":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            search = (qs.get("q") or qs.get("search") or [None])[0]
            rows = db.list_agent_memory(member_id, search=search)
            self._send(200, json.dumps({"rows": rows}).encode(), "application/json")
        elif self.command == "POST":
            try: data = self._read_json()
            except Exception: data = {}
            content = (data.get("content") or "").strip()
            if not content:
                self._send(400, b'{"error":"content is required"}', "application/json"); return
            mid = db.add_agent_memory(member_id, content,
                                      tags=data.get("tags") or [],
                                      importance=int(data.get("importance") or 5))
            db.log_agent_action(member_id, "memory_add",
                                summary=content[:200],
                                details={"memory_id": mid, "importance": data.get("importance", 5)})
            self._send(200, json.dumps({"ok": True, "id": mid}).encode(), "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _agent_memory_action(self, member_id: int, memory_id: int) -> None:
        """PUT/DELETE /api/agents/<mid>/memory/<id>"""
        if self.command == "PUT":
            try: data = self._read_json()
            except Exception: data = {}
            ok = db.update_agent_memory(memory_id, **data)
            if not ok: self._send(400, b'{"error":"bad update"}', "application/json"); return
            self._send(200, b'{"ok":true}', "application/json")
        elif self.command == "DELETE":
            ok = db.delete_agent_memory(memory_id)
            if not ok: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _agent_trigger_handler(self, member_id: int) -> None:
        """GET/POST /api/agents/<id>/triggers"""
        if self.command == "GET":
            rows = db.list_triggers(agent_id=member_id)
            self._send(200, json.dumps({"rows": rows}).encode(), "application/json")
        elif self.command == "POST":
            try: data = self._read_json()
            except Exception: data = {}
            kind = (data.get("kind") or "").strip()
            spec = (data.get("spec") or "").strip()
            if kind not in ("cron", "webhook", "event") or not spec:
                self._send(400, b'{"error":"kind in {cron,webhook,event} and spec required"}', "application/json"); return
            tid = db.add_trigger(member_id, kind, spec,
                                 prompt=data.get("prompt"),
                                 enabled=bool(data.get("enabled", True)))
            self._send(200, json.dumps({"ok": True, "id": tid}).encode(), "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _agent_trigger_action(self, member_id: int, trigger_id: int) -> None:
        """PUT/DELETE /api/agents/<mid>/triggers/<id>"""
        if self.command == "PUT":
            try: data = self._read_json()
            except Exception: data = {}
            ok = db.update_trigger(trigger_id, **data)
            if not ok: self._send(400, b'{"error":"bad update"}', "application/json"); return
            self._send(200, b'{"ok":true}', "application/json")
        elif self.command == "DELETE":
            ok = db.delete_trigger(trigger_id)
            if not ok: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _agent_logs_handler(self, member_id: int) -> None:
        """GET /api/agents/<id>/logs"""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: limit = int((qs.get("limit") or ["100"])[0])
        except Exception: limit = 100
        action = (qs.get("action") or [None])[0]
        rows = db.list_agent_logs(member_id, limit=limit, action=action)
        self._send(200, json.dumps({"rows": rows}).encode(), "application/json")

    def _agent_wake_handler(self, member_id: int) -> None:
        """POST /api/agents/<id>/wake — manually trigger an agent."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        try: data = self._read_json()
        except Exception: data = {}
        prompt = (data.get("prompt") or "").strip()
        from . import scheduler as sched_mod
        r = sched_mod._wake_agent(member_id, prompt or "(manual wake)")
        if r["ok"]:
            self._send(200, json.dumps({"ok": True, "log_id": r["log_id"], "response": r["response"]}).encode(), "application/json")
        elif r.get("error") == "provider_unavailable":
            _pe.send_result(self, r, 503, json.dumps(r).encode())
        else:
            self._send(500, json.dumps({"ok": False, "error": r.get("error")}).encode(), "application/json")

    def _agent_activity(self) -> None:
        """GET /api/agents/activity — recent activity across all agents."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: hours = int((qs.get("hours") or ["24"])[0])
        except Exception: hours = 24
        rows = db.recent_agent_activity(hours=hours)
        self._send(200, json.dumps({"rows": rows}).encode(), "application/json")

    # ============================================================
    # Webhooks: HTTP-triggered agent wake
    # ============================================================

    def _webhook_dispatch(self, token: str) -> None:
        """POST /api/webhook/<token> — external systems wake an agent via HTTP.

        The token uniquely identifies a webhook-kind trigger. Any POST body is
        appended to the agent's prompt so external systems can pass context.
        """
        if self.command != "POST":
            self._send(405, b'{"error":"POST only"}', "application/json"); return
        trigger = db.get_trigger_by_token(token)
        if not trigger:
            self._send(404, b'{"error":"unknown or disabled webhook"}', "application/json"); return
        # Read body (may be empty)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8", errors="ignore") if length else ""
        except Exception:
            raw = ""
        # Append body to prompt if present
        base_prompt = trigger.get("prompt") or ""
        if raw:
            try: data = json.loads(raw)
            except Exception: data = None
            if isinstance(data, dict):
                # Use the 'message' or 'text' field if present
                ctx = data.get("message") or data.get("text") or json.dumps(data)
            else:
                ctx = raw[:2000]
            full_prompt = f"{base_prompt}\n\n[webhook payload]\n{ctx}".strip()
        else:
            full_prompt = base_prompt or "(webhook fired)"
        # Wake agent
        from . import scheduler as sched_mod
        r = sched_mod._wake_agent(trigger["agent_id"], full_prompt, trigger_id=trigger["id"])
        if r["ok"]:
            db.record_trigger_fire(trigger["id"], status="ok")
        else:
            db.record_trigger_fire(trigger["id"], status="error", error=r.get("error"))
        self._send(200, json.dumps({"ok": r["ok"], "log_id": r.get("log_id"),
                                    "response": r.get("response", "")[:500]}).encode(),
                   "application/json")

    # ============================================================
    # Approval queue: agents propose writes, the user approves
    # ============================================================

    def _approvals(self) -> None:
        """GET /api/approvals — list pending approval items."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        rows = db.list_pending_approvals()
        self._send(200, json.dumps({"rows": rows}).encode(), "application/json")

    def _approval_action(self, approval_id: int) -> None:
        """POST /api/approvals/<id>/approve|reject — the user decides."""
        action = "approve" if self.path.endswith("/approve") else "reject"
        ok = db.resolve_approval(approval_id, action=action)
        if not ok:
            self._send(404, b'{"error":"not found"}', "application/json"); return
        self._send(200, json.dumps({"ok": True, "action": action}).encode(), "application/json")

    # ============================================================
    # SSE streaming logs (live activity feed)
    # ============================================================

    def _agent_stream(self, member_id: int) -> None:
        """GET /api/agents/<id>/stream — SSE stream of new log entries."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        # Initial: send last 5 events
        try:
            recent = db.list_agent_logs(member_id, limit=5)
            for r in reversed(recent):
                self.wfile.write(b"event: log\ndata: " + json.dumps(r).encode() + b"\n\n")
                self.wfile.flush()
        except Exception:
            pass
        # Poll for new logs every 2s, send via SSE
        last_seen_id = max((r["id"] for r in recent), default=0)
        import time as _t
        for _ in range(1800):  # max ~1h
            _t.sleep(2.0)
            try:
                rows = db.list_agent_logs(member_id, limit=20)
                new_rows = [r for r in rows if r["id"] > last_seen_id]
                for r in sorted(new_rows, key=lambda x: x["id"]):
                    self.wfile.write(b"event: log\ndata: " + json.dumps(r).encode() + b"\n\n")
                    self.wfile.flush()
                    last_seen_id = max(last_seen_id, r["id"])
                # Heartbeat
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
            except Exception:
                break

    def _activity_stream(self) -> None:
        """GET /api/agents/activity/stream — SSE stream of activity across all agents."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            recent = db.recent_agent_activity(hours=24)[:20]
            for r in reversed(recent):
                self.wfile.write(b"event: log\ndata: " + json.dumps(r).encode() + b"\n\n")
                self.wfile.flush()
        except Exception:
            pass
        last_seen_id = max((r["id"] for r in recent), default=0)
        import time as _t
        for _ in range(1800):
            _t.sleep(2.0)
            try:
                rows = db.recent_agent_activity(hours=24)[:50]
                new_rows = [r for r in rows if r["id"] > last_seen_id]
                for r in sorted(new_rows, key=lambda x: x["id"]):
                    self.wfile.write(b"event: log\ndata: " + json.dumps(r).encode() + b"\n\n")
                    self.wfile.flush()
                    last_seen_id = max(last_seen_id, r["id"])
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
            except Exception:
                break

    def _live_urls_stream(self) -> None:
        """GET /api/urls/stream — SSE stream of live URL health changes.
        Pushes updates whenever a URL is checked (every ~5min by the cron).
        Falls back to polling every 10s on the client side if no events come."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from .indexers import live_urls
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            # Send initial snapshot
            current = live_urls.latest()
            self.wfile.write(b"event: snapshot\ndata: " + json.dumps(current).encode() + b"\n\n")
            self.wfile.flush()
            seen = {r["id"]: r.get("checked_at") for r in current}
            import time as _t
            for _ in range(3600):  # max 1 hour
                _t.sleep(5.0)
                rows = live_urls.latest()
                # Find rows whose checked_at changed
                updates = [r for r in rows if seen.get(r["id"]) != r.get("checked_at")]
                if updates:
                    self.wfile.write(b"event: update\ndata: " + json.dumps(updates).encode() + b"\n\n")
                    self.wfile.flush()
                    for r in updates:
                        seen[r["id"]] = r.get("checked_at")
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            return

    def _unified_stream(self) -> None:
        """GET /api/stream — unified SSE that pushes URL health + revenue summary
        + anomalies. Sends initial snapshots then diffs every 10s.
        Event types: snapshot (initial), urls (URL changes), revenue (summary
        snapshot), anomalies (latest detection)."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from .indexers import anomalies as anomalies_mod
        from .indexers import live_urls
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        import time as _t
        try:
            # Initial snapshots
            urls_snap = live_urls.latest()
            self.wfile.write(b"event: urls\ndata: " + json.dumps(urls_snap).encode() + b"\n\n")
            self.wfile.flush()
            rev_snap = db.revenue_summary()
            self.wfile.write(b"event: revenue\ndata: " + json.dumps(rev_snap).encode() + b"\n\n")
            self.wfile.flush()
            an_snap = anomalies_mod.detect_recent_anomalies()
            self.wfile.write(b"event: anomalies\ndata: " + json.dumps(an_snap, default=str).encode() + b"\n\n")
            self.wfile.flush()
            # Track url checked_at for diffs
            seen_urls = {r["id"]: r.get("checked_at") for r in urls_snap}
            last_rev_count = rev_snap.get("count", 0)
            last_an_count = an_snap.get("count", 0)
            for _ in range(360):  # max 1 hour (10s × 360)
                _t.sleep(10.0)
                # URL diff
                rows = live_urls.latest()
                updates = [r for r in rows if seen_urls.get(r["id"]) != r.get("checked_at")]
                if updates:
                    self.wfile.write(b"event: urls\ndata: " + json.dumps(updates).encode() + b"\n\n")
                    self.wfile.flush()
                    for r in updates:
                        seen_urls[r["id"]] = r.get("checked_at")
                # Revenue diff (count-based: push only when new events)
                rev_now = db.revenue_summary()
                if rev_now.get("count", 0) != last_rev_count:
                    self.wfile.write(b"event: revenue\ndata: " + json.dumps(rev_now).encode() + b"\n\n")
                    self.wfile.flush()
                    last_rev_count = rev_now["count"]
                # Anomalies diff (count-based)
                an_now = anomalies_mod.detect_recent_anomalies()
                if an_now.get("count", 0) != last_an_count:
                    self.wfile.write(b"event: anomalies\ndata: " + json.dumps(an_now, default=str).encode() + b"\n\n")
                    self.wfile.flush()
                    last_an_count = an_now["count"]
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            return

    # ============================================================
    # Connections hub (Cloudflare / GitHub / Notion / etc. / MCP)
    # ============================================================

    def _connections_catalog(self) -> None:
        """GET /api/connections/catalog — static list of supported providers."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from . import connectors
        self._send(200, _json(connectors.list_provider_catalog()).encode(), "application/json")

    def _connections(self) -> None:
        """GET/POST /api/connections"""
        if self.command == "GET":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            provider = (qs.get("provider") or [None])[0]
            rows = db.list_connections(provider=provider)
            # Strip credentials from list response (just show what's configured)
            safe = []
            for r in rows:
                stored = r.get("credentials") or ""
                is_encrypted = stored.startswith("enc:")
                creds_keys = []
                if is_encrypted:
                    # Try to decrypt just to read the keys (without exposing values)
                    try:
                        dec = db._decrypt_credentials(stored)
                        creds_keys = list(dec.keys())
                    except Exception: pass
                elif stored.startswith("plain:"):
                    try:
                        creds_keys = list(json.loads(stored[5:]).keys())
                    except Exception: pass
                else:
                    try:
                        creds_keys = list(json.loads(stored).keys())
                    except Exception: pass
                safe.append({
                    "id": r["id"], "provider": r["provider"], "name": r["name"],
                    "status": r["status"], "enabled": bool(r["enabled"]),
                    "last_used_at": r["last_used_at"], "last_check_at": r["last_check_at"],
                    "last_error": r["last_error"],
                    "credential_keys": creds_keys,
                    "encrypted": is_encrypted,
                    "created_at": r["created_at"], "updated_at": r["updated_at"],
                })
            self._send(200, json.dumps({"rows": safe}).encode(), "application/json")
        elif self.command == "POST":
            try: data = self._read_json()
            except Exception: data = {}
            provider = (data.get("provider") or "").strip()
            name = (data.get("name") or "").strip()
            credentials = data.get("credentials") or {}
            if not provider or not name:
                self._send(400, b'{"error":"provider and name required"}', "application/json"); return
            if not isinstance(credentials, dict):
                self._send(400, b'{"error":"credentials must be object"}', "application/json"); return
            try:
                cid = db.add_connection(provider, name, credentials, config=data.get("config"))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json"); return
            self._send(200, json.dumps({"ok": True, "id": cid}).encode(), "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _connection_action(self, conn_id: int) -> None:
        """GET/PUT/DELETE /api/connections/<id>"""
        if self.command == "GET":
            c = db.get_connection_by_id(conn_id)
            if not c: self._send(404, b'{"error":"not found"}', "application/json"); return
            # Include credentials (UI needs them for re-edit) but keep server-side
            self._send(200, _json(c).encode(), "application/json")
        elif self.command == "PUT":
            try: data = self._read_json()
            except Exception: data = {}
            ok = db.update_connection(conn_id, **data)
            if not ok: self._send(400, b'{"error":"bad update"}', "application/json"); return
            self._send(200, b'{"ok":true}', "application/json")
        elif self.command == "DELETE":
            ok = db.delete_connection(conn_id)
            if not ok: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _connection_test(self, conn_id: int) -> None:
        """POST /api/connections/<id>/test — validate credentials and update status."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        c = db.get_connection_by_id(conn_id)
        if not c: self._send(404, b'{"error":"not found"}', "application/json"); return
        from . import connectors
        ok, error, info = connectors.test_connection(c["provider"], c["credentials"])
        db.record_connection_check(conn_id, ok=ok, error=error)
        if ok:
            self._send(200, json.dumps({"ok": True, "info": info}).encode(), "application/json")
        else:
            self._send(200, json.dumps({"ok": False, "error": error}).encode(), "application/json")

    def _connection_tools(self, conn_id: int) -> None:
        """GET /api/connections/<id>/tools — list tools this connection exposes."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        c = db.get_connection_by_id(conn_id)
        if not c: self._send(404, b'{"error":"not found"}', "application/json"); return
        from . import connectors
        tools = connectors.tools_for_provider(c["provider"])
        self._send(200, json.dumps({"tools": tools}).encode(), "application/json")

    # ============================================================
    # MCP servers
    # ============================================================

    def _agent_tools(self, member_id: int) -> None:
        """GET/POST/DELETE /api/agents/<id>/tools — list MCP tools bound to this agent,
        bind a new tool, or unbind one. Mirrors the memory/triggers handler pattern."""
        if self.command == "GET":
            bound = db.list_agent_tools(member_id)
            servers = db.list_mcp_servers(enabled_only=True)
            available = []
            for s in servers:
                try: tools = json.loads(s.get("tools_cache") or "[]")
                except Exception: tools = []
                available.append({"server": s["name"], "transport": s["transport"],
                                  "tools": tools, "cached_at": s.get("tools_cached_at")})
            self._send(200, json.dumps({"bound": bound, "available": available}).encode(),
                       "application/json"); return
        if self.command == "POST":
            self._agent_tool_bind(member_id); return
        if self.command == "DELETE":
            self._agent_tool_unbind(member_id); return
        self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _agent_tool_bind(self, member_id: int) -> None:
        """POST /api/agents/<id>/tools — bind a specific MCP tool to the agent.
        Body: {server: 'name', tool: 'tool_name'}."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        try: data = self._read_json()
        except Exception: data = {}
        server = (data.get("server") or "").strip()
        tool = (data.get("tool") or "").strip()
        if not server or not tool:
            self._send(400, b'{"error":"server and tool required"}', "application/json"); return
        db.bind_agent_tool(member_id, server, tool)
        db.log_agent_action(member_id, "memory_add",
                            summary=f"bound tool {server}/{tool}",
                            details={"server": server, "tool": tool})
        self._send(200, b'{"ok":true}', "application/json")

    def _agent_tool_unbind(self, member_id: int) -> None:
        """DELETE /api/agents/<id>/tools — unbind tool. Body or query: server, tool."""
        server = ""
        tool = ""
        if self.command == "DELETE":
            try: data = self._read_json()
            except Exception: data = {}
            server = data.get("server") or ""
            tool = data.get("tool") or ""
        if not server or not tool:
            self._send(400, b'{"error":"server and tool required"}', "application/json"); return
        ok = db.unbind_agent_tool(member_id, server, tool)
        if not ok:
            self._send(404, b'{"error":"not bound"}', "application/json"); return
        self._send(200, b'{"ok":true}', "application/json")

    def _mcp_servers(self) -> None:
        """GET/POST /api/mcp/servers"""
        if self.command == "GET":
            rows = db.list_mcp_servers()
            self._send(200, json.dumps({"rows": rows}).encode(), "application/json")
        elif self.command == "POST":
            try: data = self._read_json()
            except Exception: data = {}
            name = (data.get("name") or "").strip()
            transport = (data.get("transport") or "stdio").strip()
            if not name or transport not in ("stdio", "http", "sse"):
                self._send(400, b'{"error":"name required, transport in {stdio,http,sse}"}', "application/json"); return
            command = data.get("command") or []
            if isinstance(command, str):
                command = command.split()
            try:
                sid = db.upsert_mcp_server(name, transport, command=command,
                                            url=data.get("url"), env=data.get("env"))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json"); return
            self._send(200, json.dumps({"ok": True, "id": sid}).encode(), "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _mcp_server_action(self, name: str) -> None:
        """GET/PUT/DELETE /api/mcp/servers/<name>"""
        if self.command == "GET":
            s = db.get_mcp_server(name)
            if not s: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, _json(s).encode(), "application/json")
        elif self.command == "DELETE":
            # Through the hub, so keychain secrets, policies, the connector
            # account and pending actions go too (not just the row).
            from . import mcp_hub
            try:
                mcp_hub.remove_server(name)
            except mcp_hub.HubError as e:
                if e.status == 404:
                    self._send(404, b'{"error":"not found"}', "application/json"); return
                raise
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _mcp_discover(self, name: str) -> None:
        """POST /api/mcp/servers/<name>/discover — connect and list tools."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        s = db.get_mcp_server(name)
        if not s: self._send(404, b'{"error":"not found"}', "application/json"); return
        from . import mcp as mcp_mod
        try:
            tools, error = mcp_mod.discover_tools(name, s["transport"],
                                                  command=s.get("command"),
                                                  url=s.get("url"),
                                                  env=s.get("env"))
        except Exception as e:
            error = f"discover exception: {type(e).__name__}: {e}"
            tools = None
        if error:
            db.set_mcp_server_error(name, error)
            self._send(200, json.dumps({"ok": False, "error": error}).encode(), "application/json")
            return
        db.cache_mcp_tools(name, tools or [])
        self._send(200, json.dumps({"ok": True, "tools": tools or []}).encode(), "application/json")

    # ============================================================
    # Revenue sources + events
    # ============================================================

    def _revenue_sources(self) -> None:
        """GET/POST /api/revenue/sources — list or add a revenue source."""
        if self.command == "GET":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            rows = db.list_revenue_sources(
                project_key=(qs.get("project") or [None])[0],
                source_type=(qs.get("type") or [None])[0],
                enabled_only=(qs.get("enabled_only") or ["0"])[0] in ("1", "true", "yes"),
            )
            self._send(200, json.dumps({"rows": rows}).encode(), "application/json")
        elif self.command == "POST":
            try: data = self._read_json()
            except Exception: data = {}
            name = (data.get("name") or "").strip()
            source_type = (data.get("source_type") or "").strip()
            project_key = (data.get("project_key") or "").strip()
            connection_id = data.get("connection_id")
            currency = (data.get("currency") or "USD").upper()
            if not name or not source_type or not project_key:
                self._send(400, b'{"error":"name, source_type, project_key required"}', "application/json"); return
            existing = db.get_revenue_source_by_name(name)
            if existing:
                self._send(409, json.dumps({"error": "name already exists", "id": existing["id"]}).encode(), "application/json"); return
            sid = db.add_revenue_source(
                name=name, source_type=source_type, project_key=project_key,
                connection_id=int(connection_id) if connection_id else None,
                currency=currency,
            )
            self._send(200, json.dumps({"ok": True, "id": sid}).encode(), "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _revenue_source_action(self, source_id: int) -> None:
        """GET/PUT/DELETE /api/revenue/sources/<id>"""
        if self.command == "GET":
            row = db.get_revenue_source(source_id)
            if not row: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, json.dumps(row, default=str).encode(), "application/json")
        elif self.command == "PUT":
            try: data = self._read_json()
            except Exception: data = {}
            ok = db.update_revenue_source(source_id, **data)
            if not ok: self._send(400, b'{"error":"bad update"}', "application/json"); return
            self._send(200, b'{"ok":true}', "application/json")
        elif self.command == "DELETE":
            ok = db.delete_revenue_source(source_id)
            if not ok: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _revenue_source_test(self, source_id: int) -> None:
        """POST /api/revenue/sources/<id>/test — test connection without inserting."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from .indexers import revenue
        ok, message = revenue.test_source(source_id)
        self._send(200, json.dumps({"ok": ok, "message": message}).encode(), "application/json")

    def _revenue_source_poll(self, source_id: int) -> None:
        """POST /api/revenue/sources/<id>/poll — fetch new events from this source."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        row = db.get_revenue_source(source_id)
        if not row: self._send(404, b'{"error":"not found"}', "application/json"); return
        from .indexers import revenue
        result = revenue.run(source_name=row["name"])
        self._send(200, json.dumps(result, default=str).encode(), "application/json")

    def _revenue_events(self) -> None:
        """GET /api/revenue/events — list with filters."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: limit = int((qs.get("limit") or ["100"])[0])
        except Exception: limit = 100
        try: since = int((qs.get("since") or [None])[0]) if qs.get("since") else None
        except Exception: since = None
        try: until = int((qs.get("until") or [None])[0]) if qs.get("until") else None
        except Exception: until = None
        rows = db.list_revenue_events(
            project_key=(qs.get("project") or [None])[0],
            source_type=(qs.get("type") or [None])[0],
            event_type=(qs.get("event") or [None])[0],
            status=(qs.get("status") or [None])[0],
            since=since, until=until, limit=limit,
        )
        self._send(200, json.dumps({"rows": rows}).encode(), "application/json")

    def _revenue_forecast_api(self) -> None:
        """GET /api/revenue/forecast?days=30&project=X — linear projection."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: days = int((qs.get("days") or ["30"])[0])
        except Exception: days = 30
        project = (qs.get("project") or [None])[0]
        result = db.revenue_forecast(days=days, project_key=project)
        self._send(200, json.dumps(result, default=str).encode(), "application/json")

    def _ga_forecast_api(self) -> None:
        """GET /api/ga/forecast?metric=users|page_views&days=30 — linear projection."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: days = int((qs.get("days") or ["30"])[0])
        except Exception: days = 30
        metric = (qs.get("metric") or ["users"])[0]
        project = (qs.get("project") or [None])[0]
        result = db.ga_forecast(days=days, metric=metric, project_key=project)
        self._send(200, json.dumps(result, default=str).encode(), "application/json")

    def _sentry_projects(self) -> None:
        """GET/POST /api/sentry/projects — list or add a Sentry project."""
        if self.command == "GET":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            rows = db.list_sentry_projects(
                project_key=(qs.get("project") or [None])[0],
                enabled_only=(qs.get("enabled_only") or ["0"])[0] in ("1", "true", "yes"),
            )
            # Don't leak auth_token in list
            for r in rows:
                r.pop("auth_token", None)
            self._send(200, json.dumps({"rows": rows}, default=str).encode(), "application/json")
        elif self.command == "POST":
            try: data = self._read_json()
            except Exception: data = {}
            name = (data.get("name") or "").strip()
            project_key = (data.get("project_key") or "").strip()
            if not name or not project_key:
                self._send(400, b'{"error":"name, project_key required"}', "application/json"); return
            try:
                sid = db.add_sentry_project(
                    name=name, source_type=data.get("source_type", "sentry"),
                    project_key=project_key,
                    sentry_project=data.get("sentry_project"),
                    org_slug=data.get("org_slug"),
                    auth_token=data.get("auth_token"),
                    enabled=bool(data.get("enabled", True)),
                )
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json"); return
            self._send(200, json.dumps({"ok": True, "id": sid}).encode(), "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _sentry_project_action(self, sid: int) -> None:
        """GET/PUT/DELETE /api/sentry/projects/<id>"""
        if self.command == "GET":
            row = db.get_sentry_project(sid)
            if not row: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, json.dumps(row, default=str).encode(), "application/json")
        elif self.command == "PUT":
            try: data = self._read_json()
            except Exception: data = {}
            ok = db.update_sentry_project(sid, **data)
            if not ok: self._send(400, b'{"error":"bad update"}', "application/json"); return
            self._send(200, b'{"ok":true}', "application/json")
        elif self.command == "DELETE":
            ok = db.delete_sentry_project(sid)
            if not ok: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _sentry_project_test(self, sid: int) -> None:
        """POST /api/sentry/projects/<id>/test — test connection."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from .indexers import sentry as sentry_idx
        ok, message = sentry_idx.test_project(sid)
        self._send(200, json.dumps({"ok": ok, "message": message}).encode(), "application/json")

    def _sentry_project_poll(self, sid: int) -> None:
        """POST /api/sentry/projects/<id>/poll — fetch issues."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: days = int((qs.get("days") or ["7"])[0])
        except Exception: days = 7
        from .indexers import sentry as sentry_idx
        result = sentry_idx.run(project_id=sid, days=days)
        self._send(200, json.dumps(result, default=str).encode(), "application/json")

    def _sentry_issues(self) -> None:
        """GET /api/sentry/issues — list with filters."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: limit = int((qs.get("limit") or ["200"])[0])
        except Exception: limit = 200
        try: since = int((qs.get("since") or [None])[0]) if qs.get("since") else None
        except Exception: since = None
        rows = db.list_sentry_issues(
            project_key=(qs.get("project") or [None])[0],
            level=(qs.get("level") or [None])[0],
            status=(qs.get("status") or [None])[0],
            since=since, limit=limit,
        )
        self._send(200, json.dumps({"rows": rows}, default=str).encode(), "application/json")

    def _sentry_summary(self) -> None:
        """GET /api/sentry/summary — totals + breakdowns."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: since = int((qs.get("since") or [None])[0]) if qs.get("since") else None
        except Exception: since = None
        result = db.sentry_summary(since=since)
        self._send(200, json.dumps(result, default=str).encode(), "application/json")

    def _sentry_seed_mock(self) -> None:
        """POST /api/sentry/seed-mock — create one mock source per known project + poll."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from . import projects as proj_mod
        created = []
        for p in proj_mod.PROJECTS:
            name = f"sentry-mock-{p.key}"
            if not db.get_sentry_project_by_name(name):
                sid = db.add_sentry_project(name=name, source_type="mock", project_key=p.key)
                created.append({"name": name, "id": sid})
        from .indexers import sentry as sentry_idx
        poll_result = sentry_idx.run(days=7)
        self._send(200, json.dumps({"created": created, "poll": poll_result}, default=str).encode(), "application/json")

    def _anomalies_api(self) -> None:
        """GET /api/anomalies — recent revenue / GA anomalies (spikes/drops vs 7d baseline)."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from .indexers import anomalies
        result = anomalies.detect_recent_anomalies()
        self._send(200, json.dumps(result, default=str).encode(), "application/json")

    def _cohorts_api(self) -> None:
        """GET /api/revenue/cohorts?project=X&months=6 — customer retention cohort table."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: months = int((qs.get("months") or ["6"])[0])
        except Exception: months = 6
        project = (qs.get("project") or [None])[0]
        result = db.customer_cohorts(project_key=project, months=months)
        self._send(200, json.dumps(result, default=str).encode(), "application/json")

    def _changelog_page(self) -> None:
        """GET /changelog — rendered changelog page."""
        data = _changelog_data()
        parts = ['<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">',
                '<title>Changelog — ConductLoop</title>',
                '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
                '<meta name="theme-color" content="#0f172a">',
                '<style>body{margin:0;font-family:-apple-system,sans-serif;background:#0f172a;color:#f1f5f9;line-height:1.6}',
                '.container{max-width:780px;margin:0 auto;padding:48px 24px 80px}',
                'h1{font-size:32px;margin:0 0 8px 0;font-weight:800}',
                '.tagline{color:#94a3b8;margin:0 0 32px 0}',
                '.release{background:#0b1220;border:1px solid #334155;border-radius:12px;padding:24px;margin-bottom:16px}',
                '.release.current{border-color:#fbbf24}',
                '.version{font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}',
                '.current-tag{background:#fbbf24;color:#0f172a;padding:2px 8px;border-radius:4px;margin-left:6px;font-size:10px;font-weight:700}',
                '.release h2{margin:0 0 8px 0;font-size:20px}',
                '.release .date{color:#94a3b8;font-size:13px;margin-bottom:12px}',
                '.release ul{margin:8px 0;padding-left:20px}',
                '.release li{padding:3px 0;color:#cbd5e1}',
                'a{color:#3b82f6;text-decoration:none} a:hover{text-decoration:underline}',
                '.nav{display:flex;gap:14px;margin-bottom:24px;font-size:13px}',
                '</style></head><body><div class="container">',
                '<h1>📜 Changelog</h1>',
                '<p class="tagline">Every release of ConductLoop, with what changed and why.</p>',
                '<div class="nav"><a href="/">← Dashboard</a><a href="/tour">Quick tour</a></div>']
        for i, r in enumerate(data):
            cls = "release current" if i == 0 else "release"
            cur = '<span class="current-tag">CURRENT</span>' if i == 0 else ""
            parts.append('<div class="' + cls + '">')
            parts.append('<div class="version">v' + r["version"] + cur + '</div>')
            parts.append('<h2>' + r["title"] + '</h2>')
            parts.append('<div class="date">' + r["date"] + '</div>')
            parts.append('<ul>')
            for h in r["highlights"]:
                parts.append('<li>' + h + '</li>')
            parts.append('</ul></div>')
        parts.append('</div></body></html>')
        self._send(200, ''.join(parts).encode(), "text/html; charset=utf-8")

    def _status_json(self) -> None:
        """GET /api/status.json — public status JSON (no auth, for status page)."""
        from .indexers import live_urls
        rows = live_urls.latest()
        # Find max checked_at
        checked_at = max((r.get("checked_at") for r in rows if r.get("checked_at")), default=0)
        self._send(200, json.dumps({"rows": rows, "checked_at": checked_at}, default=str).encode(), "application/json",
                   headers={"Cache-Control": "no-cache, must-revalidate", "Access-Control-Allow-Origin": "*"})

    def _status_page(self) -> None:
        """GET /status — public status page (HTML)."""
        path = STATIC_DIR / "status.html"
        if not path.exists():
            self._send(404, b"status page not found", "text/plain"); return
        body = path.read_bytes()
        self._send(200, body, "text/html", headers={"Cache-Control": "no-cache, must-revalidate"})

    def _i18n_serve(self, api_path: str) -> None:
        """GET /api/i18n/<locale>.json — serve a translation JSON file.

        The browser's i18n.js loader fetches /api/i18n/en.json or es.json.
        We serve the same files from web/i18n/<locale>.json.
        """
        # api_path looks like '/api/i18n/en.json' -> strip '/api' to get '/i18n/en.json'
        rel = api_path[len("/api"):]
        full = STATIC_DIR / rel.lstrip("/")
        if not full.exists():
            self._send(404, b'{"error":"locale not found"}', "application/json"); return
        try:
            body = full.read_bytes()
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json"); return
        self._send(200, body, "application/json; charset=utf-8",
                   headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"})

    def _notes_ai_action(self, doc_id: int) -> None:
        """POST /api/notes/<id>/ai-action — run AI transformation on a note."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        try: data = self._read_json()
        except Exception: data = {}
        action = data.get("action") or ""
        target_lang = data.get("lang") or data.get("target_lang") or "es"
        provider = data.get("provider") or None
        from .indexers import note_ai
        result = note_ai.run_action(doc_id, action, target_lang=target_lang, provider_name=provider)
        _pe.send_result(self, result, 200, json.dumps(result, default=str).encode())

    def _notes_save(self, doc_id: int) -> None:
        """POST /api/notes/<id>/save — update note content."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        try: data = self._read_json()
        except Exception: data = {}
        content = data.get("content") or ""
        if not isinstance(content, str):
            self._send(400, b'{"error":"content must be a string"}', "application/json"); return
        ok = db.update_document_content(doc_id, content)
        if not ok:
            self._send(404, b'{"error":"doc not found"}', "application/json"); return
        try:
            db.add_activity("note_saved", f"Doc {doc_id} updated via dashboard")
        except Exception:
            pass
        self._send(200, json.dumps({"ok": True, "doc_id": doc_id, "chars": len(content)}).encode(), "application/json")

    def _notes_get(self, doc_id: int) -> None:
        """GET /api/notes/<id> — fetch a single note (used by the editor)."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        row = db.get_document(doc_id)
        if not row:
            self._send(404, b'{"error":"not found"}', "application/json"); return
        # Don't expose huge content in list views; this endpoint is for the editor so size is OK
        self._send(200, json.dumps(row, default=str).encode(), "application/json")

    def _notes_backlinks(self, doc_id: int) -> None:
        """GET /api/notes/<id>/backlinks — docs that link TO this doc."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        links = db.get_backlinks(doc_id)
        self._send(200, json.dumps({"doc_id": doc_id, "backlinks": links, "count": len(links)}).encode(), "application/json")

    def _notes_links(self, doc_id: int) -> None:
        """GET /api/notes/<id>/links — docs that this doc links TO."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        links = db.get_outgoing_links(doc_id)
        self._send(200, json.dumps({"doc_id": doc_id, "links": links, "count": len(links)}).encode(), "application/json")

    def _digest_ai_summary_api(self) -> None:
        """GET /api/digest/ai-summary?date=YYYY-MM-DD — regenerate AI daily summary."""
        from urllib.parse import parse_qs, urlparse

        from .indexers import ai_summary as ai_mod
        qs = parse_qs(urlparse(self.path).query)
        target_str = (qs.get("date") or [None])[0]
        target = None
        if target_str:
            try:
                import datetime as _dt
                target = _dt.date.fromisoformat(target_str)
            except Exception:
                self._send(400, b'{"error":"bad date format (YYYY-MM-DD)"}', "application/json"); return
        try:
            result = ai_mod.generate_ai_summary(target, provider_name=(qs.get("provider") or [None])[0])
            self._send(200, json.dumps(result, default=str).encode(), "application/json")
        except _pe.ProviderUnavailableError as e:
            _pe.send(self, e)
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def _digest_api(self) -> None:
        """GET /api/digest?date=YYYY-MM-DD — build/send daily digest.
        POST /api/digest — same as GET but with body {date, send: bool}."""
        from urllib.parse import parse_qs, urlparse

        from .indexers import digest as digest_mod
        if self.command == "GET":
            qs = parse_qs(urlparse(self.path).query)
            target_str = (qs.get("date") or [None])[0]
            target = None
            if target_str:
                try:
                    import datetime as _dt
                    target = _dt.date.fromisoformat(target_str)
                except Exception:
                    self._send(400, b'{"error":"bad date format (YYYY-MM-DD)"}', "application/json"); return
            path = digest_mod.save_digest(target)
            md = path.read_text(encoding="utf-8")
            self._send(200, json.dumps({"date": (target or digest_mod.dt.date.today() - digest_mod.dt.timedelta(days=1)).isoformat(),
                                       "path": str(path), "markdown": md}, default=str).encode(), "application/json")
        elif self.command == "POST":
            try: data = self._read_json()
            except Exception: data = {}
            target_str = data.get("date")
            target = None
            if target_str:
                import datetime as _dt
                target = _dt.date.fromisoformat(target_str)
            send = bool(data.get("send", True))
            path = digest_mod.save_digest(target)
            md = path.read_text(encoding="utf-8")
            send_result = None
            if send:
                send_result = digest_mod.send_digest(md, target_date=target)
            self._send(200, json.dumps({"path": str(path), "send": send_result}, default=str).encode(), "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _revenue_margin(self) -> None:
        """GET /api/revenue/margin — per-project revenue / costs / margin."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: since = int((qs.get("since") or [None])[0]) if qs.get("since") else None
        except Exception: since = None
        try: until = int((qs.get("until") or [None])[0]) if qs.get("until") else None
        except Exception: until = None
        rows = db.revenue_margin_by_project(since=since, until=until)
        # Add grand total
        total = {
            "project_key": "TOTAL",
            "revenue_cents": sum(r.get("revenue_cents", 0) or 0 for r in rows),
            "revenue_count": sum(r.get("revenue_count", 0) or 0 for r in rows),
            "cost_cents": sum(r.get("cost_cents", 0) or 0 for r in rows),
            "cost_count": sum(r.get("cost_count", 0) or 0 for r in rows),
            "margin_cents": 0,
            "margin_pct": None,
        }
        total["margin_cents"] = total["revenue_cents"] - total["cost_cents"]
        if total["revenue_cents"] > 0:
            total["margin_pct"] = round((total["margin_cents"] / total["revenue_cents"]) * 100, 1)
        rows.append(total)
        self._send(200, json.dumps({"rows": rows}, default=str).encode(), "application/json")

    def _cost_seed_mock(self) -> None:
        """POST /api/cost/seed-mock — create realistic monthly costs per project
        for the current and previous 2 months. Idempotent (skips existing periods)."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        try: data = self._read_json()
        except Exception: data = {}
        months = int(data.get("months") or 3)
        # Realistic cost templates per project (monthly, in cents)
        from datetime import datetime, timedelta
        # Fictional projects for demo/testing — no personal data.
        cost_templates = {
            "mosaic":   [("stripe", "fees", 22000, "Stripe fees ~2.9%"),
                         ("vercel", "infra", 2000, "Vercel Pro"),
                         ("cloudflare", "infra", 500, "Workers + Pages"),
                         ("apple", "infra", 800, "Apple Developer"),
                         ("resend", "api", 100, "Resend email"),
                         ("github", "tools", 400, "GitHub Pro")],
            "verdant":  [("cloudflare", "infra", 500, "Workers + Pages"),
                         ("google", "infra", 3000, "Cloud Run"),
                         ("github", "tools", 0, "GitHub free")],
        }
        # Use project_key -> name mapping for any project in cost_templates
        now = datetime.now()
        created = []
        for project_key, items in cost_templates.items():
            for m_offset in range(months):
                d = (now - timedelta(days=30 * m_offset))
                period = d.strftime("%Y-%m")
                billed_at = int(d.timestamp())
                # Skip if a cost for this (project, period, provider) already exists
                existing = db.list_costs(period=period, project_key=project_key, limit=10)
                for provider, category, amount_cents, desc in items:
                    if any(c["provider"] == provider and c["period"] == period for c in existing):
                        continue
                    cid = db.add_cost(
                        provider=provider, amount_cents=amount_cents,
                        period=period, project_key=project_key,
                        category=category, description=desc,
                        billed_at=billed_at,
                    )
                    created.append({"id": cid, "project": project_key, "provider": provider, "period": period, "eur": amount_cents/100})
        self._send(200, json.dumps({"created": len(created), "items": created[:20]}, default=str).encode(), "application/json")

    def _revenue_summary(self) -> None:
        """GET /api/revenue/summary — aggregate totals + breakdowns."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: since = int((qs.get("since") or [None])[0]) if qs.get("since") else None
        except Exception: since = None
        try: until = int((qs.get("until") or [None])[0]) if qs.get("until") else None
        except Exception: until = None
        result = db.revenue_summary(
            project_key=(qs.get("project") or [None])[0],
            source_type=(qs.get("type") or [None])[0],
            since=since, until=until,
        )
        self._send(200, json.dumps(result, default=str).encode(), "application/json")

    def _revenue_daily(self) -> None:
        """GET /api/revenue/daily — time-series, one bucket per day."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: since = int((qs.get("since") or [None])[0]) if qs.get("since") else None
        except Exception: since = None
        try: until = int((qs.get("until") or [None])[0]) if qs.get("until") else None
        except Exception: until = None
        rows = db.revenue_daily_buckets(
            project_key=(qs.get("project") or [None])[0],
            source_type=(qs.get("type") or [None])[0],
            since=since, until=until,
        )
        self._send(200, json.dumps({"rows": rows}).encode(), "application/json")

    def _revenue_seed_mock(self) -> None:
        """POST /api/revenue/seed-mock — create one mock source per project for testing/demo.
        Body (optional): {projects: ['mosaic','verdant'], currency: 'USD'}"""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        try: data = self._read_json()
        except Exception: data = {}
        projects = data.get("projects") or ["mosaic", "verdant"]
        currency = (data.get("currency") or "USD").upper()
        created = []
        for project in projects:
            name = f"mock-{project}"
            existing = db.get_revenue_source_by_name(name)
            if existing:
                created.append({"name": name, "id": existing["id"], "existed": True})
                continue
            sid = db.add_revenue_source(name=name, source_type="mock", project_key=project, currency=currency)
            created.append({"name": name, "id": sid, "existed": False})
        # Poll all mock sources to populate events
        from .indexers import revenue as rev_idx
        poll_result = rev_idx.run()
        self._send(200, json.dumps({"created": created, "poll": poll_result}, default=str).encode(), "application/json")

    # ============================================================
    # GA4 sites + metrics
    # ============================================================

    def _ga_sites(self) -> None:
        """GET/POST /api/ga/sites — list or add a GA property."""
        if self.command == "GET":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            rows = db.list_ga_sites(
                project_key=(qs.get("project") or [None])[0],
                enabled_only=(qs.get("enabled_only") or ["0"])[0] in ("1", "true", "yes"),
            )
            # Don't leak service_account_json in list
            for r in rows:
                r.pop("service_account_json", None)
            self._send(200, json.dumps({"rows": rows}, default=str).encode(), "application/json")
        elif self.command == "POST":
            try: data = self._read_json()
            except Exception: data = {}
            property_id = (data.get("property_id") or "").strip()
            display_name = (data.get("display_name") or "").strip()
            project_key = (data.get("project_key") or "").strip()
            if not property_id or not display_name or not project_key:
                self._send(400, b'{"error":"property_id, display_name, project_key required"}', "application/json"); return
            try:
                sid = db.add_ga_site(
                    property_id=property_id, display_name=display_name, project_key=project_key,
                    url=data.get("url"),
                    service_account_json=data.get("service_account_json"),
                    enabled=bool(data.get("enabled", True)),
                )
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json"); return
            self._send(200, json.dumps({"ok": True, "id": sid}).encode(), "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _ga_site_action(self, site_id: int) -> None:
        """GET/PUT/DELETE /api/ga/sites/<id>"""
        if self.command == "GET":
            row = db.get_ga_site(site_id)
            if not row: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, json.dumps(row, default=str).encode(), "application/json")
        elif self.command == "PUT":
            try: data = self._read_json()
            except Exception: data = {}
            ok = db.update_ga_site(site_id, **data)
            if not ok: self._send(400, b'{"error":"bad update"}', "application/json"); return
            self._send(200, b'{"ok":true}', "application/json")
        elif self.command == "DELETE":
            ok = db.delete_ga_site(site_id)
            if not ok: self._send(404, b'{"error":"not found"}', "application/json"); return
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(405, b'{"error":"method not allowed"}', "application/json")

    def _ga_site_test(self, site_id: int) -> None:
        """POST /api/ga/sites/<id>/test — test connection without fetching."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from .indexers import ga as ga_idx
        ok, message = ga_idx.test_site(site_id)
        self._send(200, json.dumps({"ok": ok, "message": message}).encode(), "application/json")

    def _ga_site_poll(self, site_id: int) -> None:
        """POST /api/ga/sites/<id>/poll — fetch metrics for this site."""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        row = db.get_ga_site(site_id)
        if not row: self._send(404, b'{"error":"not found"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: days = int((qs.get("days") or ["7"])[0])
        except Exception: days = 7
        from .indexers import ga as ga_idx
        result = ga_idx.run(site_id=site_id, days=days)
        self._send(200, json.dumps(result, default=str).encode(), "application/json")

    def _ga_metrics(self) -> None:
        """GET /api/ga/metrics — daily metrics with filters."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        try: limit = int((qs.get("limit") or ["365"])[0])
        except Exception: limit = 365
        try: site_id = int((qs.get("site") or [None])[0]) if qs.get("site") else None
        except Exception: site_id = None
        rows = db.list_ga_metrics_daily(
            project_key=(qs.get("project") or [None])[0],
            site_id=site_id,
            since=(qs.get("since") or [None])[0],
            until=(qs.get("until") or [None])[0],
            limit=limit,
        )
        self._send(200, json.dumps({"rows": rows}, default=str).encode(), "application/json")

    def _ga_summary(self) -> None:
        """GET /api/ga/summary — totals + per-site breakdown."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        result = db.ga_summary(
            project_key=(qs.get("project") or [None])[0],
            since=(qs.get("since") or [None])[0],
            until=(qs.get("until") or [None])[0],
        )
        self._send(200, json.dumps(result, default=str).encode(), "application/json")

    def _ga_daily(self) -> None:
        """GET /api/ga/daily — time-series totals across sites."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        rows = db.ga_daily_totals(
            project_key=(qs.get("project") or [None])[0],
            since=(qs.get("since") or [None])[0],
            until=(qs.get("until") or [None])[0],
        )
        self._send(200, json.dumps({"rows": rows}, default=str).encode(), "application/json")

    def _gsc_sites(self) -> None:
        """GET /api/gsc/sites — list Search Console properties."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        rows = db.list_gsc_sites(
            project_key=(qs.get("project") or [None])[0],
            enabled_only=(qs.get("enabled_only") or ["0"])[0] in ("1", "true", "yes"),
        )
        # Don't leak credentials_json in list
        for r in rows:
            r.pop("credentials_json", None)
        self._send(200, json.dumps({"rows": rows}, default=str).encode(), "application/json")

    def _gsc_summary(self) -> None:
        """GET /api/gsc/summary — totals + per-site breakdown."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        result = db.gsc_summary(
            project_key=(qs.get("project") or [None])[0],
            since=(qs.get("since") or [None])[0],
            until=(qs.get("until") or [None])[0],
        )
        self._send(200, json.dumps(result, default=str).encode(), "application/json")

    def _gsc_daily(self) -> None:
        """GET /api/gsc/daily — per-day rows (optionally per site/project)."""
        if self.command != "GET":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        sid = (qs.get("site") or [None])[0]
        rows = db.gsc_daily(
            site_id=int(sid) if sid else None,
            project_key=(qs.get("project") or [None])[0],
            since=(qs.get("since") or [None])[0],
            until=(qs.get("until") or [None])[0],
        )
        self._send(200, json.dumps({"rows": rows}, default=str).encode(), "application/json")

    def _ga_seed_mock(self) -> None:
        """POST /api/ga/seed-mock — create one mock GA site per known URL/project for dev/demo.
        Body (optional): {projects: [{key,url,name}], days: 30}"""
        if self.command != "POST":
            self._send(405, b'{"error":"method not allowed"}', "application/json"); return
        try: data = self._read_json()
        except Exception: data = {}
        days = int(data.get("days") or 30)
        # Default: every known project gets a mock GA site
        from . import projects as proj_mod
        sites = data.get("projects") or [
            {"key": p.key, "name": p.name, "url": f"https://{p.key}.example.com"}
            for p in proj_mod.PROJECTS
        ]
        created = []
        for s in sites:
            key = s.get("key") if isinstance(s, dict) else s
            name = (s.get("name") if isinstance(s, dict) else s) or key
            url = s.get("url") if isinstance(s, dict) else f"https://{key}.example.com"
            prop_id = f"mock-{key}"
            # Skip if exists
            existing = None
            for r in db.list_ga_sites():
                if r["property_id"] == prop_id:
                    existing = r; break
            if existing:
                created.append({"property_id": prop_id, "id": existing["id"], "existed": True})
                continue
            sid = db.add_ga_site(
                property_id=prop_id, display_name=name, project_key=key, url=url,
            )
            created.append({"property_id": prop_id, "id": sid, "existed": False})
        from .indexers import ga as ga_idx
        poll_result = ga_idx.run(days=days)
        self._send(200, json.dumps({"created": created, "poll": poll_result}, default=str).encode(), "application/json")

    def _add_report(self, path: str) -> None:
        """Add a new sub-agent reporting to an existing member (POST /api/members/<id>/reports)."""
        m = re.match(r"^/api/members/(\d+)/reports$", path)
        if not m:
            self._send(404, b"not found"); return
        manager_id = int(m.group(1))
        with db.db() as conn:
            parent = conn.execute("SELECT * FROM team_members WHERE id = ?", (manager_id,)).fetchone()
        if not parent:
            self._send(404, b'{"error":"manager not found"}', "application/json"); return
        data = self._read_json()
        name = (data.get("name") or "").strip()
        if not name:
            self._send(400, b'{"error":"name is required"}', "application/json"); return
        # Inherit team_id, type, capabilities from parent
        role  = (data.get("role")  or "agent").strip()
        level = (data.get("level") or "agent").strip()
        if level not in db.LEVEL_ORDER:
            level = "agent"
        type_ = (data.get("type") or parent["type"]).strip()
        model = (data.get("model") or "").strip() or None
        parent_caps = parent["capabilities"] if parent["capabilities"] else ""
        own_caps    = (data.get("capabilities") or "").strip()
        if own_caps and parent_caps:
            caps = f"{parent_caps}\n\nSub-agent specialization:\n{own_caps}"
        elif own_caps:
            caps = own_caps
        else:
            caps = parent_caps or None
        status = (data.get("status") or "active").strip()
        try:
            with db.db() as conn:
                cur = conn.execute(
                    "INSERT INTO team_members (team_id, name, type, role, status, model, capabilities, level, manager_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (parent["team_id"], name, type_, role, status, model, caps, level, manager_id,
                     int(__import__('time').time()), int(__import__('time').time())),
                )
                new_id = int(cur.lastrowid or 0)
        except Exception as e:
            self._send(500, _json({"ok": False, "error": str(e)}).encode(), "application/json"); return
        # Get the full row
        with db.db() as conn:
            row = conn.execute("SELECT * FROM team_members WHERE id = ?", (new_id,)).fetchone()
        # Convert sqlite3.Row to dict via the get() workaround
        member_dict = {k: row[k] for k in row.keys()} if row else {}
        self._send(200, _json({"ok": True, "id": new_id, "member": member_dict}).encode(), "application/json")

    def _add_entry(self, project_key: str, kind: str, content: str, done: bool) -> None:
        """Append an entry to the playbook .md file under the right section,
        then trigger a re-index."""
        from .indexers import playbook as playbook_indexer
        pb_path = playbook_indexer.PLAYBOOK_DIR / f"{project_key}.md"
        if not pb_path.exists():
            pb_path.parent.mkdir(parents=True, exist_ok=True)
            pb_path.write_text(f"# {project_key}\n\n**Phase:** Co-Pilots\n\n", encoding="utf-8")
        text = pb_path.read_text(encoding="utf-8")
        # Map kind to section heading
        kind_to_section = {
            "agent_in_use":    "AI agents in use",
            "proposed_agent":  "Proposed agents (next 90 days)",
            "risk":            "Risk & compliance",
            "blocker":         "Open blockers",
            "win":             "Wins to date",
            "note":            "Cross-references",
            "cross_ref":       "Cross-references",
        }
        section = kind_to_section.get(kind, "Cross-references")
        # If section doesn't exist, append it.
        section_header = f"## {section}"
        if section_header not in text:
            text = text.rstrip() + f"\n\n{section_header}\n\n"
        # Insert under section
        idx = text.index(section_header) + len(section_header)
        # find end of section (next '## ' or EOF)
        rest = text[idx:]
        end = len(text)
        for marker in ("\n## ", "\n# "):
            pos = rest.find(marker)
            if pos != -1 and idx + pos < end:
                end = idx + pos
        # Build new line
        checkbox = "x" if done else " "
        new_line = f"- [{checkbox}] {content}\n"
        new_text = text[:end] + new_line + text[end:]
        pb_path.write_text(new_text, encoding="utf-8")
        # Reindex
        playbook_indexer.run()

    def _update_entry(self, project_key: str, entry_id: int, data: dict) -> None:
        from .indexers import playbook as playbook_indexer
        with db.db() as conn:
            row = conn.execute(
                "SELECT entry_kind, content, done, order_idx FROM playbook_entries WHERE id = ? AND project_key = ?",
                (entry_id, project_key),
            ).fetchone()
            if not row:
                raise ValueError(f"entry {entry_id} not found in {project_key}")
        pb_path = playbook_indexer.PLAYBOOK_DIR / f"{project_key}.md"
        if not pb_path.exists():
            return
        text = pb_path.read_text(encoding="utf-8")
        # The DB row has the up-to-date done/content (from a prior index run).
        # We always read the .md, toggle exactly that line, and reindex.
        # Use the DB row to find the line text to replace.
        cur_done = row["done"]
        cur_content = row["content"]
        if "done" in data:
            target_done = 1 if data["done"] else 0
        else:
            target_done = cur_done
        target_content = data.get("content", cur_content)
        if cur_done == target_done and cur_content == target_content:
            return  # no-op
        # Find the line in the .md matching `- [x] <content>` or `- [ ] <content>`
        import re
        pattern = re.compile(
            r"^- \[[ xX]\] " + re.escape(cur_content) + r"\s*$",
            re.MULTILINE,
        )
        new_line = f"- [{'x' if target_done else ' '}] {target_content}"
        new_text, n = pattern.subn(new_line, text, count=1)
        if n:
            pb_path.write_text(new_text, encoding="utf-8")
        playbook_indexer.run()


# ============================================================
# Changelog data + page
# ============================================================

def _changelog_data():
    return [
        {
            "version": "0.5.0",
            "date": "2026-06-22",
            "title": "AI-native second brain — multi-language + lifetime billing",
            "highlights": [
                "Multi-language i18n (EN + ES) with locale picker",
                "Auth (bcrypt + cookie sessions) + lifetime $299 / Pro $12 / Team $49 plans",
                "AI inline en notas: summarize, translate, action_items (in web editor)",
                "Backlinks detection (wiki, markdown, mentions) — 1,131 vault links indexed",
                "Daily AI exec summary by Ollama/Anthropic/OpenAI",
                "Revenue + GA4 + Sentry + URL health + alerts banner + brain context",
                "Mobile-responsive UI + public status page Worker + SSE stream",
                "4 LaunchAgents auto-polling (URLs 5min, revenue 1h, GA 1d, digest 8am)",
                "audit-before-ship skill gates every commit",
            ],
        },
        {
            "version": "0.4.0",
            "date": "2026-06-15",
            "title": "Revenue dashboard + Stripe + Apple + Google Play",
            "highlights": [
                "3 source indexers with mock + real (Stripe REST API, App Store Connect JWT, Google Play service account)",
                "Encrypted connections at rest (Fernet)",
                "Anomaly detection (z-score vs 7d baseline)",
                "Margin by project (revenue − costs)",
                "Linear regression forecasts (revenue + GA users)",
                "Customer cohort retention table",
            ],
        },
        {
            "version": "0.3.0",
            "date": "2026-06-01",
            "title": "Brain + agents + tools",
            "highlights": [
                "Brain with Anthropic / OpenAI / Ollama / MiniMax providers",
                "Persistent memory (preferences, brain_memory)",
                "Activity feed with kinds: task_started, agent_artifact, task_done, todo_added",
                "Agent hierarchy (CEO/VP/Director/Manager/Agent)",
                "Playbook phases + entries",
                "Voice transcription via faster-whisper",
            ],
        },
        {
            "version": "0.2.0",
            "date": "2026-05-15",
            "title": "Multi-source indexer",
            "highlights": [
                "Obsidian vault, git repos, memory files, live URLs",
                "FTS5 + semantic search (sentence-transformers)",
                "Live URL health checks with anomaly tracking",
            ],
        },
        {
            "version": "0.1.0",
            "date": "2026-05-01",
            "title": "First release",
            "highlights": [
                "Single binary local-first dashboard",
                "Org chart of projects",
                "Basic search across vault",
            ],
        },
    ]


def _resolve_bind(host: str, port: int) -> str:
    """Opt-in LAN binding for mobile pairing. If OPS_ALLOW_MOBILE=1 and the host
    is still the loopback default, bind 0.0.0.0 and print LAN URLs + pairing hint.
    Default stays localhost-only (private)."""
    allow = os.environ.get("OPS_ALLOW_MOBILE") == "1"
    if allow and host == "127.0.0.1":
        host = "0.0.0.0"
    if allow:
        import socket
        ips: set[str] = set()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80)); ips.add(s.getsockname()[0]); s.close()
        except Exception:
            pass
        urls = [f"http://{ip}:{port}" for ip in sorted(ips) if not ip.startswith("127.")]
        console.print("[bold cyan]▸ MOBILE ACCESS ON[/bold cyan] (OPS_ALLOW_MOBILE=1) — bound to LAN")
        for u in urls:
            console.print(f"    {u}")
        console.print(f"    Pair a phone at [cyan]http://127.0.0.1:{port}/pair[/cyan]")
    return host


@app.callback(invoke_without_command=True)
def _default(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(7777, help="Bind port"),
) -> None:
    """Run the local web dashboard (default if no subcommand given)."""
    if ctx.invoked_subcommand is not None:
        return
    host = _resolve_bind(host, port)
    db.init_db()
    live_urls.ensure_schema()
    # ── Demo mode: load fixture data when CONDUCTLOOP_DEMO=1 ────────────
    if os.environ.get("CONDUCTLOOP_DEMO") == "1":
        from .indexers import demo as _demo_mod
        _demo_db = os.environ.get("CONDUCTLOOP_DB_PATH")
        if _demo_db:
            from pathlib import Path as _Path

            from . import db as _db_mod
            _db_mod.DB_PATH = _Path(_demo_db)  # type: ignore[attr-defined]
            db.init_db()
        result = _demo_mod.run()
        console.print(f"[bold magenta]▸ DEMO MODE[/bold magenta] — fixture data loaded: {result}")
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    if os.environ.get("CONDUCTLOOP_DEMO") == "1":
        console.print(f"[bold green]✓[/bold green] ops-dashboard (DEMO) serving on [cyan]http://{host}:{port}/[/cyan]")
    else:
        console.print(f"[bold green]✓[/bold green] ops-dashboard serving on [cyan]http://{host}:{port}/[/cyan]")
    console.print("  Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[yellow]shutting down[/yellow]")
    finally:
        server.shutdown()


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(7777, help="Bind port"),
    with_scheduler: bool = typer.Option(True, "--with-scheduler/--no-scheduler", help="Start the agent scheduler thread"),
    scheduler_interval: float = typer.Option(30.0, help="Scheduler tick interval in seconds"),
) -> None:
    """Alias for the default action."""
    host = _resolve_bind(host, port)
    db.init_db()
    live_urls.ensure_schema()
    # ── Demo mode: load fixture data and skip the real scheduler ────────
    if os.environ.get("CONDUCTLOOP_DEMO") == "1":
        from .indexers import demo as _demo_mod
        # Override DB path so demo never touches your real ops.db
        _demo_db = os.environ.get("CONDUCTLOOP_DB_PATH")
        if _demo_db:
            from pathlib import Path as _Path

            from . import db as _db_mod
            _db_mod.DB_PATH = _Path(_demo_db)  # type: ignore[attr-defined]
            db.init_db()
        result = _demo_mod.run()
        console.print(f"[bold magenta]\u25b8 DEMO MODE[/bold magenta] \u2014 fixture data loaded: {result}")
    scheduler = None
    if with_scheduler and os.environ.get("CONDUCTLOOP_DEMO") != "1":
        from . import scheduler as sched_mod
        scheduler = sched_mod.Scheduler(interval=scheduler_interval)
        scheduler.start()
        console.print(f"[green]\u2713[/green] agent scheduler running (every {scheduler_interval:.0f}s)")
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    if os.environ.get("CONDUCTLOOP_DEMO") == "1":
        console.print(f"[bold green]\u2713[/bold green] ops-dashboard (DEMO) serving on [cyan]http://{host}:{port}/[/cyan]")
    else:
        console.print(f"[bold green]\u2713[/bold green] ops-dashboard serving on [cyan]http://{host}:{port}/[/cyan]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[yellow]shutting down[/yellow]")
    finally:
        server.shutdown()
        if scheduler:
            scheduler.stop()


if __name__ == "__main__":
    app()
