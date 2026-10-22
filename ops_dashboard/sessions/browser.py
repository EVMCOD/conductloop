"""Agent browser (browser design PR 8 read-only, PR 9 click/type; B7/B10/B11/B12).

* ``BrowserBridge``: client of the desktop app's Unix socket (Rust,
  ``agent_bridge.rs``). Path and token come from the environment the app gives
  the sidecar; they are popped from ``os.environ`` on first use, so no child
  process (git, sandboxed code tools…) inherits them.
* ``BrowserTools``: the toolset a browser session may call, gated by the
  session's ``browser_mode`` (``read`` → ``open``/``navigate``/``read``;
  ``act`` adds ``click``/``type`` on a ``ref`` from the last read). No eval,
  no free selectors, no screenshots to the model (B12). Everything read from a
  page is returned wrapped as untrusted (``web_ask.wrap_untrusted``).
* Effects (B10, ``browser_effects``): a deterministic classifier, never the
  model, decides. ``deny`` (passwords…) is refused; ``confirm`` creates an
  approval in the existing queue (``agent_logs`` / ``/api/approvals``) bound to
  the session, URL, ref, element, text and DOM hash. The agent waits; once
  approved, the approval is consumed (single use) and the page is read again:
  if the DOM hash changed the approval is invalidated and nothing happens.
  Rust re-classifies the live element and refuses an effectful action that
  arrives without ``confirmed``.
* New sites (B10): the agent only goes to registrable domains opened in the
  session — seeded from the user's task and messages, grown by approvals and
  by what the user opens while in control. Going anywhere else (``open``,
  ``navigate``, or a click/redirect started by the page, which Rust stops and
  reports as ``pending_nav``) asks the user through the same queue, bound to
  the current page and single use; Rust refuses an unconfirmed navigation to
  a new site and does not read or act on a page that landed off-site.
* ``create_browser_session`` / ``set_browser_mode``: browser sessions have no
  repo (``repo_path`` NULL, migration 20) — a session is either code or
  browser, never both. Only the human changes ``browser_mode`` (local API).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from . import browser_effects as fx
from . import limits
from .limits import SessionLimitError, SessionValidationError
from .store import SessionStore

SOCK_ENV, TOKEN_ENV = "CONDUCTLOOP_BROWSER_SOCK", "CONDUCTLOOP_BROWSER_TOKEN"
BROWSER_MODES = ("off", "read", "act")
READ_TOOLS = ("open", "navigate", "read")
ACT_TOOLS = ("click", "type")            # PR 9
MAX_RESPONSE = 4 * 1024 * 1024
MAX_SNAPSHOT_TEXT = 12_000
MAX_ELEMENTS = 150
MAX_TYPE_CHARS = 2000                    # same cap as agent_bridge.rs
APPROVAL_TIMEOUT_S = 600.0
APPROVAL_POLL_S = 1.0
APPROVAL_PREFIX = "browser."


class BrowserUnavailable(RuntimeError):
    """No desktop bridge (web-only install, macOS < 14, app not running)."""


class BrowserDenied(PermissionError):
    """Tool not allowed by the session's browser_mode."""


_cfg_lock = threading.Lock()
_cfg: dict[str, str | None] = {}


def bridge_config() -> tuple[str | None, str | None]:
    """(socket path, token). Popped from the environment once, then cached."""
    with _cfg_lock:
        if not _cfg:
            _cfg["sock"] = os.environ.pop(SOCK_ENV, None) or None
            _cfg["token"] = os.environ.pop(TOKEN_ENV, None) or None
        return _cfg["sock"], _cfg["token"]


def _reset_config_for_tests() -> None:
    with _cfg_lock:
        _cfg.clear()


class BrowserBridge:
    def __init__(self, path: str | None = None, token: str | None = None, *, timeout: float = 30.0):
        if path is None or token is None:
            p, t = bridge_config()
            path, token = path or p, token or t
        self.path, self.token, self.timeout = path, token, timeout

    @property
    def available(self) -> bool:
        return bool(self.path and self.token)

    def call(self, op: str, session: int, **args: Any) -> dict:
        if not self.available:
            raise BrowserUnavailable("the in-app browser is only available in the ConductLoop "
                                     "desktop app (macOS 14+)")
        req = json.dumps({"token": self.token, "op": op, "session": int(session), **args})
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(str(self.path))
            s.sendall(req.encode("utf-8") + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_RESPONSE:
                    raise BrowserUnavailable("bridge response too large")
        except OSError as e:
            raise BrowserUnavailable(f"browser bridge: {e.__class__.__name__}") from None
        finally:
            s.close()
        try:
            out = json.loads(buf.decode("utf-8"))
        except ValueError:
            raise BrowserUnavailable("browser bridge: bad response") from None
        return out if isinstance(out, dict) else {"ok": False, "error": "bad_response"}


def allowed_tools(mode: str) -> tuple[str, ...]:
    if mode == "read":
        return READ_TOOLS
    if mode == "act":
        return READ_TOOLS + ACT_TOOLS
    return ()


def format_snapshot(snap: dict) -> tuple[str, str]:
    """Snapshot → (text for the model, DOM hash). Field values are never
    included (the page script only reports ``filled``); capped."""
    def s(v: Any, n: int) -> str:
        return str(v or "").replace("\n", " ")[:n]

    lines = [f"url: {s(snap.get('url'), 500)}", f"title: {s(snap.get('title'), 300)}"]
    hid = web_clip_hidden(snap.get("hidden"))
    if hid:
        lines.append(f"note: {hid} invisible element(s) with text were left out of this read")
    if snap.get("has_password"):
        lines.append("note: this page has a password field (the agent never types passwords; "
                     "ask the user to take control)")
    raw = snap.get("elements")
    els: list = raw if isinstance(raw, list) else []
    lines.append("interactive elements:")
    for e in els[:MAX_ELEMENTS]:
        if not isinstance(e, dict):
            continue
        bits = [f"[{int(e.get('ref') or 0)}]", s(e.get("tag"), 20)]
        for k in ("type", "role"):
            if e.get(k):
                bits.append(f"{k}={s(e.get(k), 30)}")
        if e.get("in_form"):
            bits.append("in_form")
        if e.get("payment"):
            bits.append("payment")
        if e.get("filled"):
            bits.append("filled")
        bits.append(json.dumps(s(e.get("name"), 120), ensure_ascii=False))
        if e.get("href"):
            bits.append("→ " + s(e.get("href"), 300))
        lines.append("  " + " ".join(bits))
    lines.append("text:")
    lines.append(str(snap.get("text") or "")[:MAX_SNAPSHOT_TEXT])
    if snap.get("truncated"):
        lines.append("[truncated]")
    text = "\n".join(lines)
    dom_hash = hashlib.sha256(json.dumps(
        [snap.get("url"), [(e.get("ref"), e.get("tag"), e.get("name"), e.get("type"))
                           for e in els if isinstance(e, dict)]],
        sort_keys=True, default=str).encode()).hexdigest()[:32]
    return text, dom_hash


class BrowserTools:
    """Browser tools of ONE session. Mode is re-read from the DB on every call,
    so the human can downgrade it while the session runs."""

    def __init__(self, store: SessionStore, session_id: int, *, bridge: BrowserBridge | None = None,
                 cancel: threading.Event | None = None,
                 approval_timeout: float | None = None,
                 on_wait: Callable[[], None] | None = None):
        self.store, self.session_id = store, int(session_id)
        self.bridge = bridge or BrowserBridge()
        self.last_hash: str | None = None
        self.last_snapshot: dict | None = None
        self.cancel = cancel or threading.Event()
        self.approval_timeout = APPROVAL_TIMEOUT_S if approval_timeout is None else approval_timeout
        self.on_wait = on_wait
        self.sites: set[str] = set()
        self.refused_sites: set[str] = set()

    def seed_sites(self, text: str) -> None:
        """Sites the USER named (task, messages) count as opened."""
        for host in _hosts_in(str(text or "")):
            if len(self.sites) < MAX_SEED_SITES:
                self.sites.add(fx.registrable_domain(host))

    def _tab(self) -> dict:
        st = self.status()
        raw = st.get("tab")
        tab: dict = raw if isinstance(raw, dict) else {}
        for site in tab.get("sites") or []:
            if isinstance(site, str) and site:
                self.sites.add(site)       # Rust's list: approvals + what the user opened
        return tab

    def mode(self) -> str:
        s = self.store.get_session(self.session_id) or {}
        m = str(s.get("browser_mode") or "off")
        return m if m in BROWSER_MODES else "off"

    def _check(self, tool: str) -> None:
        if tool not in allowed_tools(self.mode()):
            self.store.record_event(self.session_id, "browser.denied", {"tool": tool,
                                                                         "mode": self.mode()})
            raise BrowserDenied(f"{tool} is not allowed in browser_mode={self.mode()}")

    def status(self) -> dict:
        return self.bridge.call("status", self.session_id)

    def open(self, url: str) -> dict:
        self._check("open")
        return self._nav("open", url)

    def navigate(self, url: str) -> dict:
        self._check("navigate")
        return self._nav("navigate", url)

    def _nav(self, op: str, url: str) -> dict:
        if not isinstance(url, str) or not url.strip() or len(url) > 2048:
            return {"ok": False, "error": "url required"}
        target = _with_scheme(url.strip())
        self._tab()
        site = fx.nav_needs_confirm(self.sites, target)
        if site is None:
            r = self._nav_call(op, target)
            err = str(r.get("error") or "")
            if not err.startswith("confirmation_required:"):
                return self._navved(op, target, r, None)
            site = fx.nav_needs_confirm([], target) or "?"
        return self._confirm_nav(op, target, site, cause="agent")

    def _nav_call(self, op: str, url: str, **kw: Any) -> dict:
        return self.bridge.call(op, self.session_id, url=url, sites=sorted(self.sites)[:MAX_SEED_SITES],
                                **kw)

    def _navved(self, op: str, url: str, r: dict, aid: int | None) -> dict:
        self.store.record_event(self.session_id, f"browser.{op}",
                                {"url": url[:500], "ok": bool(r.get("ok")), "error": r.get("error"),
                                 **({"approval_id": aid} if aid else {})})
        if r.get("ok"):
            self.last_snapshot = None
        return {"ok": bool(r.get("ok")), **({"error": r["error"]} if r.get("error") else {})}

    def _binding(self) -> str:
        """What a navigation approval is bound to: the page as it is now."""
        tab = self._tab()
        if not tab.get("open"):
            return "no-tab"
        if tab.get("off_site"):
            return "off-site " + str(tab.get("url") or "")
        snap = self._snapshot()
        return "page " + (snap[1] if snap else "unreadable " + str(tab.get("url") or ""))

    def _confirm_nav(self, op: str, url: str, site: str, *, cause: str) -> dict:
        """Ask the user before going to a site not opened in the session."""
        if site in self.refused_sites:
            return {"ok": False, "error": f"the user already refused {site} in this task"}
        bound = self._binding()
        base = {"op": "navigate" if op != "open" else "open", "url": url[:2048], "site": site,
                "cause": cause}
        aid = add_browser_approval(self.store, self.session_id,
                                   {**base, "effect": "confirm:new_site", "bound_to": bound},
                                   reason=f"Browser agent wants to go to a new site: {site}"
                                          + (" (the page tried to navigate)" if cause == "page" else ""))
        self.store.record_event(self.session_id, "browser.approval_pending",
                                {**base, "effect": "confirm:new_site", "approval_id": aid})
        decision = self._wait_approval(aid)
        if decision != "approved":
            self.store.record_event(self.session_id, "browser.approval_" + decision,
                                    {**base, "approval_id": aid})
            if decision == "rejected":
                self.refused_sites.add(site)
            msg = {"rejected": f"the user did not allow going to {site}",
                   "expired": "nobody approved it in time", "cancelled": "cancelled"}.get(decision, decision)
            return {"ok": False, "error": msg}
        if not consume_browser_approval(self.store, aid, self.session_id):
            return {"ok": False, "error": "approval already used"}
        if self._binding() != bound:
            self.store.record_event(self.session_id, "browser.approval_invalidated",
                                    {**base, "approval_id": aid})
            return {"ok": False, "error": "the page changed after approval; nothing was done"}
        r = self._nav_call(op, url, confirmed=True)
        if r.get("ok"):
            self.sites.add(site)
        return self._navved(op, url, r, aid)

    def _pending_nav(self) -> dict | None:
        """A navigation the page started (link, JS, form, redirect) that Rust
        stopped because its site is new: ask the user now."""
        tab = self._tab()
        pending = tab.get("pending_nav")
        if not tab.get("open") or not isinstance(pending, str) or not pending:
            return None
        site = fx.nav_needs_confirm(self.sites, pending)
        if site is None:
            return None
        r = self._confirm_nav("navigate", pending, site, cause="page")
        return {"url": pending[:500], **r}

    def _snapshot(self) -> tuple[dict, str] | None:
        r = self.bridge.call("read", self.session_id)
        if not r.get("ok"):
            return None
        raw = r.get("snapshot")
        snap: dict = raw if isinstance(raw, dict) else {}
        return snap, format_snapshot(snap)[1]

    def read(self) -> dict:
        self._check("read")
        nav = self._pending_nav()
        r = self.bridge.call("read", self.session_id)
        if not r.get("ok"):
            err = str(r.get("error") or "read failed")
            if err == "off_site":
                err = ("this tab is on a site that was not opened in the session; it is not read. "
                       "Navigate back to a known site")
            return {"ok": False, "error": err, **({"navigation": nav} if nav else {})}
        raw = r.get("snapshot")
        snap: dict = raw if isinstance(raw, dict) else {}
        text, h = format_snapshot(snap)
        self.last_hash, self.last_snapshot = h, snap
        from .. import web_clip
        self.store.record_event(self.session_id, "browser.read",
                                {"url": str(snap.get("url") or "")[:500], "dom_hash": h,
                                 "elements": len(snap.get("elements") or []),
                                 "hidden_dropped": web_clip.hidden_summary(
                                     web_clip._page_hidden(snap.get("hidden")), {})})
        return {"ok": True, "page": text, "dom_hash": h}


    # ── click / type (PR 9) ─────────────────────────────────────────────────

    def _act(self, op: str, ref: Any, text: str | None = None, submit: bool = False) -> dict:
        """click/type on ``ref`` from the last read, with B10 confirmations."""
        self._check(op)
        if isinstance(ref, str) and ref.strip().isdigit():
            ref = int(ref.strip())
        if isinstance(ref, bool) or not isinstance(ref, int) or ref <= 0:
            return {"ok": False, "error": "ref: a number from the last read"}
        if op == "type" and (not isinstance(text, str) or len(text) > MAX_TYPE_CHARS):
            return {"ok": False, "error": f"text: a string of at most {MAX_TYPE_CHARS} characters"}
        snap, h = self.last_snapshot, self.last_hash
        if snap is None or h is None:
            return {"ok": False, "error": "read the page first"}
        el = _element(snap, ref)
        if el is None:
            return {"ok": False, "error": "no element with that ref in the last read"}
        effect = fx.classify(op, fx.element_from_snapshot(el), _page_of(snap), submit=bool(submit))
        base = {"op": op, "ref": ref, "url": str(snap.get("url") or "")[:500]}
        if effect.startswith("deny:"):
            self.store.record_event(self.session_id, "browser.denied", {**base, "effect": effect})
            return {"ok": False, "error": f"refused ({effect[5:]}): the agent never does this; "
                                           "ask the user to take control"}
        args: dict[str, Any] = {"ref": ref}
        if op == "type":
            args.update(text=text, submit=bool(submit))
        if effect == "allow":
            r = self.bridge.call(op, self.session_id, **args)
            err = str(r.get("error") or "")
            if not err.startswith("confirmation_required:"):
                return self._acted(op, base, effect, r)
            # The live element is more sensitive than the snapshot said: confirm.
            effect = "confirm:" + err.split(":", 1)[1][:40]
        # Confirm: the page must still be the one the agent read.
        fresh = self._snapshot()
        if fresh is None or fresh[1] != h:
            self.last_snapshot = None
            return {"ok": False, "error": "the page changed since your last read: read it again"}
        aid = add_browser_approval(self.store, self.session_id, {
            **base, "effect": effect, "dom_hash": h,
            "element": {k: el.get(k) for k in ("tag", "type", "role", "name", "href", "in_form",
                                                "payment") if el.get(k) not in (None, "", False)},
            **({"text": text, "submit": bool(submit)} if op == "type" else {})},
            reason=_describe(op, el, text, effect))
        self.store.record_event(self.session_id, "browser.approval_pending",
                                {**base, "effect": effect, "approval_id": aid})
        decision = self._wait_approval(aid)
        if decision != "approved":
            self.store.record_event(self.session_id, "browser.approval_" + decision,
                                    {**base, "approval_id": aid})
            msg = {"rejected": "the user rejected this action", "expired": "nobody approved it in time",
                   "cancelled": "cancelled"}.get(decision, decision)
            return {"ok": False, "error": msg}
        if not consume_browser_approval(self.store, aid, self.session_id):
            return {"ok": False, "error": "approval already used"}
        fresh = self._snapshot()
        if fresh is None or fresh[1] != h:
            self.last_snapshot = None
            self.store.record_event(self.session_id, "browser.approval_invalidated",
                                    {**base, "approval_id": aid})
            return {"ok": False, "error": "the page changed after approval; nothing was done. "
                                          "Read it again"}
        r = self.bridge.call(op, self.session_id, **args, confirmed=True)
        return self._acted(op, {**base, "approval_id": aid}, effect, r)


    def _acted(self, op: str, base: dict, effect: str, r: dict) -> dict:
        ok = bool(r.get("ok"))
        self.store.record_event(self.session_id, f"browser.{op}",
                                {**base, "effect": effect, "ok": ok, "error": r.get("error")})
        # Whatever happened, the agent must read again before the next action.
        self.last_snapshot = None
        out: dict[str, Any] = {"ok": ok, **({"error": str(r["error"])[:200]} if r.get("error") else {})}
        if ok:
            out["note"] = "done; read the page again before the next action"
            nav = self._pending_nav()
            if nav:
                out["navigation"] = nav
        return out


    def _wait_approval(self, aid: int) -> str:
        deadline = time.monotonic() + self.approval_timeout
        while not self.cancel.is_set():
            st = browser_approval_status(self.store, aid)
            if st in ("approved", "rejected"):
                return st
            if time.monotonic() >= deadline:
                expire_browser_approval(self.store, aid)
                return "expired"
            if self.on_wait:
                self.on_wait()
            self.cancel.wait(APPROVAL_POLL_S)
        expire_browser_approval(self.store, aid, status="cancelled")
        return "cancelled"


    def click(self, ref: Any) -> dict:
        return self._act("click", ref)


    def type(self, ref: Any, text: Any, submit: Any = False) -> dict:
        return self._act("type", ref, text if isinstance(text, str) else None, bool(submit))


def create_browser_session(store: SessionStore, *, name: str, role: str, provider: str, model: str,
                           budget_cost_cents: int, browser_mode: str = "read",
                           max_iterations: int = limits.DEFAULT_MAX_ITERATIONS,
                           key_ref: str | None = None, max_active: int | None = None) -> int:
    """A browser-only session: no repo, no worktree, no code tools."""
    for field, val in (("name", name), ("role", role), ("provider", provider), ("model", model)):
        if not isinstance(val, str) or not val.strip():
            raise SessionValidationError(field, "required")
    if browser_mode not in ("read", "act"):
        raise SessionValidationError("browser_mode", "must be 'read' or 'act'")
    limits.validate_budget(budget_cost_cents, limits.BUDGET_CURRENCY, None)
    limits.validate_iterations(max_iterations, store._max_iterations_limit)
    key_ref = limits.validate_key_ref(key_ref, provider)
    cap = limits.resolve_max_active(max_active)
    active_sql = ",".join(f"'{s}'" for s in limits.ACTIVE_SESSION_STATUSES)
    with store._tx() as c:
        active = c.execute(
            f"SELECT COUNT(*) FROM agent_sessions WHERE status IN ({active_sql})").fetchone()[0]
        if active >= cap:
            raise SessionLimitError(f"{active} active sessions; limit is {cap}")
        now = store._now()
        cur = c.execute(
            "INSERT INTO agent_sessions (name, role, scope_globs, provider, model, key_ref, "
            "repo_path, base_ref, budget_currency, budget_cost_cents, max_iterations, "
            "browser_mode, created_at, updated_at) "
            "VALUES (?, ?, '[]', ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)",
            (name.strip(), role.strip(), provider.strip(), model.strip(), key_ref,
             limits.BUDGET_CURRENCY, budget_cost_cents, max_iterations, browser_mode, now, now))
        sid = int(cur.lastrowid or 0)
    store.record_event(sid, "session.created", {"provider": provider, "model": model,
                                                "browser_mode": browser_mode})
    return sid


def set_browser_mode(store: SessionStore, session_id: int, mode: str) -> bool:
    """Human-only (local API). A browser session can go read ⇄ act; 'off'
    is not allowed for it (it has no repo) — close it instead."""
    if mode not in ("read", "act"):
        raise SessionValidationError("browser_mode", "must be 'read' or 'act'")
    with store._tx() as c:
        n = c.execute("UPDATE agent_sessions SET browser_mode = ?, updated_at = ? "
                      "WHERE id = ? AND browser_mode <> 'off'",
                      (mode, store._now(), int(session_id))).rowcount
    if n:
        store.record_event(int(session_id), "browser.mode", {"mode": mode, "by": "user"})
    return bool(n)


def is_browser_session(s: dict | None) -> bool:
    return bool(s) and str((s or {}).get("browser_mode") or "off") != "off"


# ── click / type helpers (PR 9) ──────────────────────────────────────────────

MAX_SEED_SITES = 32                      # same cap as agent_bridge.rs
_URL_HOST_RE = re.compile(r"\bhttps?://([^/\s?#:@]+)", re.I)
_BARE_HOST_RE = re.compile(r"(?<![\w@.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])",
                           re.I)


def _hosts_in(text: str) -> list[str]:
    hosts = [m.group(1) for m in _URL_HOST_RE.finditer(text)]
    rest = _URL_HOST_RE.sub(" ", text)
    return hosts + [m.group(1) for m in _BARE_HOST_RE.finditer(rest)]


def _with_scheme(url: str) -> str:
    """Like ``browser::normalize_input``: no scheme → https://."""
    m = re.match(r"^([a-z][a-z0-9+.\-]*):(.?)", url, re.I)
    if m and "." not in m.group(1) and not m.group(2).isdigit():
        return url
    return "https://" + url


def web_clip_hidden(v: Any) -> int:
    """Total invisible elements the page script left out (clamped)."""
    from .. import web_clip
    return sum(web_clip._page_hidden(v).values())


def _page_of(snap: dict) -> fx.Page:
    host = urlsplit(str(snap.get("url") or "")).hostname or ""
    return fx.Page(host=host, has_password=bool(snap.get("has_password")),
                   has_payment=bool(snap.get("has_payment")))


def _element(snap: dict, ref: int) -> dict | None:
    for e in snap.get("elements") or []:
        if isinstance(e, dict) and e.get("ref") == ref:
            return e
    return None


def _describe(op: str, el: dict, text: str | None, effect: str) -> str:
    name = str(el.get("name") or el.get("tag") or "element")[:80]
    what = f'click "{name}"' if op == "click" else f'type into "{name}"'
    if op == "type" and text is not None:
        what += f": {text[:80]!r}"
    return f"Browser agent wants to {what} ({effect.split(':', 1)[-1]})"


# Approvals live in the existing queue (agent_logs rows with
# action='approval_proposed', same JSON as db_approvals) so /api/approvals and
# its UI show them; written through the session store's connection. agent_id
# is 0 (sessions are not team members); the session id is in the payload.

def add_browser_approval(store: SessionStore, session_id: int, payload: dict, *, reason: str) -> int:
    action = APPROVAL_PREFIX + str(payload.get("op"))
    with store._tx() as c:
        cur = c.execute(
            "INSERT INTO agent_logs (agent_id, action, summary, details, created_at) "
            "VALUES (0, 'approval_proposed', ?, ?, ?)",
            (reason[:300], json.dumps({"approval_action": action, "status": "pending",
                                       "approval_payload": {**payload, "session": int(session_id)}}),
             int(store._now())))
        return int(cur.lastrowid or 0)


def _approval_row(c: Any, aid: int) -> dict | None:
    row = c.execute("SELECT details FROM agent_logs WHERE id = ? AND action = 'approval_proposed'",
                    (int(aid),)).fetchone()
    if not row:
        return None
    try:
        d = json.loads(row[0] or "{}")
    except ValueError:
        return None
    if not str(d.get("approval_action") or "").startswith(APPROVAL_PREFIX):
        return None
    return d if isinstance(d, dict) else None


def browser_approval_status(store: SessionStore, aid: int) -> str:
    with store._tx() as c:
        d = _approval_row(c, aid)
    return str((d or {}).get("status") or "missing")


def _update(store: SessionStore, aid: int, fn: Callable[[dict], bool]) -> bool:
    with store._tx() as c:
        d = _approval_row(c, aid)
        if d is None or not fn(d):
            return False
        c.execute("UPDATE agent_logs SET details = ? WHERE id = ?", (json.dumps(d), int(aid)))
        return True


def consume_browser_approval(store: SessionStore, aid: int, session_id: int) -> bool:
    """Single use: approved → used, atomically (BEGIN IMMEDIATE)."""
    def fn(d: dict) -> bool:
        p = d.get("approval_payload") or {}
        if d.get("status") != "approved" or d.get("used_at") or p.get("session") != int(session_id):
            return False
        d["used_at"] = int(store._now())
        return True
    return _update(store, aid, fn)


def expire_browser_approval(store: SessionStore, aid: int, *, status: str = "expired") -> bool:
    def fn(d: dict) -> bool:
        if d.get("status") != "pending":
            return False
        d["status"], d["resolved_at"] = status, int(store._now())
        return True
    return _update(store, aid, fn)


def resolve_browser_approval(store: SessionStore, aid: int, session_id: int, decision: str) -> bool:
    """Human decision from the agent bar; only pending approvals of that session."""
    if decision not in ("approve", "reject"):
        return False

    def fn(d: dict) -> bool:
        p = d.get("approval_payload") or {}
        if d.get("status") != "pending" or p.get("session") != int(session_id):
            return False
        d["status"] = "approved" if decision == "approve" else "rejected"
        d["resolved_at"] = int(store._now())
        return True
    return _update(store, aid, fn)


def pending_browser_approvals(store: SessionStore, session_id: int) -> list[dict]:
    with store._tx() as c:
        rows = c.execute(
            "SELECT id, summary, details, created_at FROM agent_logs "
            "WHERE action = 'approval_proposed' AND json_extract(details, '$.status') = 'pending' "
            "AND json_extract(details, '$.approval_payload.session') = ? ORDER BY id",
            (int(session_id),)).fetchall()
    out = []
    for r in rows:
        try:
            d = json.loads(r[2] or "{}")
        except ValueError:
            continue
        if str(d.get("approval_action") or "").startswith(APPROVAL_PREFIX):
            out.append({"id": r[0], "summary": r[1], "created_at": r[3],
                        "payload": d.get("approval_payload") or {}})
    return out
