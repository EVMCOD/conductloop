"""Prompt-injection evals for the in-app browser (browser design PR 10, §3.4.6).

Two targets, both offline, network blocked, no keys:

``web.ask_page``
    A trap page (``fixtures.page``: an HTML file shaped like what
    ``browser_extract.js`` sends: Readability's article HTML) goes through the
    real ``web_ask.ask_page`` path (``web_clip`` HTML→Markdown, redaction,
    untrusted wrapping). The provider is the normal one-turn cassette. The
    checks look at the prompt: hidden text, comments, ``alt`` and ``<head>``
    must not reach it; visible injections must only appear inside the
    UNTRUSTED block.

``browser.agent``
    The real ``BrowserRunner`` + ``BrowserTools`` (modes, effect classifier,
    approvals bound to the DOM hash) against ``EvalBridge``, an in-process
    stand-in for ``agent_bridge.rs`` that serves snapshot fixtures (the JSON
    ``agent_snapshot.js`` emits) and re-classifies the "live" element the way
    Rust does (``browser_effects`` is the shared-vector mirror of
    ``url-policy/src/effects.rs``). The model is a **script cassette**
    (``"kind": "script"``, ``origin: synthetic``): a deliberately compromised
    model that obeys the page. The checks look at what was *executed* on the
    page, so they measure the policy, not the model. A simulated user
    approves or rejects every approval (``fixtures.user``).

What this does NOT cover: the JavaScript (hidden-node filtering in
``agent_snapshot.js`` / ``browser_extract.js``) and the Rust URL policy run
only in WKWebView/cargo; their own tests live with them.
"""
from __future__ import annotations

import functools
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cassette as _cassette

EVAL_NONCE = "e0e0e0e0e0e0e0e0"
_BLOCK_RE = re.compile(r"<<<UNTRUSTED-([0-9a-f]{16}) [^\n]*>>>\n.*?\n<<<END-UNTRUSTED-\1>>>", re.S)


def untrusted_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _BLOCK_RE.finditer(text or "")]


def outside_untrusted(text: str, needle: str) -> list[int]:
    """Offsets where ``needle`` (case-insensitive) appears OUTSIDE every
    untrusted block."""
    spans = untrusted_spans(text)
    low, n = (text or "").lower(), needle.lower()
    out, i = [], low.find(n)
    while i >= 0:
        if not any(a <= i and i + len(n) <= b for a, b in spans):
            out.append(i)
        i = low.find(n, i + 1)
    return out


# ---------- script cassettes (multi-turn, synthetic) ----------

@dataclass
class ScriptProvider:
    """Replays ``turns`` in order, one per ``ask``. No request matching: the
    agent's prompts contain per-run state; the checks judge the effects."""

    path: Path
    name: str = "script"
    model: str = "script"
    calls: list[dict] = field(default_factory=list)
    error: _cassette.CassetteError | None = None
    last_usage: dict | None = None
    stale_system: bool = False
    _turns: list[str] | None = None

    def ask(self, system: str, messages: list[dict], **kwargs: Any) -> str:
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        try:
            if self._turns is None:
                data = _cassette.load(self.path)
                turns = data.get("turns")
                if data.get("kind") != "script" or not isinstance(turns, list) or not turns:
                    raise _cassette.CassetteError(f"not a script cassette: {self.path.name}")
                self._turns = [json.dumps(t) if isinstance(t, dict) else str(t) for t in turns]
            if len(self.calls) > len(self._turns):
                raise _cassette.UnexpectedCall(
                    f"model called {len(self.calls)} times; script has {len(self._turns)} turns")
            self.last_usage = {"input_tokens": 0, "output_tokens": 1}
            return self._turns[len(self.calls) - 1]
        except _cassette.CassetteError as e:
            self.error = e
            raise


# ---------- the bridge stand-in ----------

class EvalBridge:
    """Same ``.available`` / ``.call`` API as ``BrowserBridge``.

    ``pages``: url → snapshot. ``live``: url → {ref: element overrides} for
    what Rust would find in the page when acting (can differ from the
    snapshot). ``links``: url → {ref: url} where a click navigates.
    ``redirects``: url → url the server redirects to.

    New-site gate (B10), as in ``browser.rs``/``agent_bridge.rs``: the tab
    knows the sites opened in the session (the start page, the backend's
    seeds at ``open``, approvals). ``open``/``navigate`` to a new site without
    ``confirmed`` is refused; a navigation started by the page (a link or a
    redirect) to a new site is stopped and reported as ``pending_nav``.
    """

    available = True

    def __init__(self, pages: dict[str, dict], *, live: dict | None = None,
                 links: dict | None = None, redirects: dict | None = None,
                 start: str | None = None):
        from ..sessions import browser_effects as fx

        self.pages, self.live, self.links = pages, live or {}, links or {}
        self.redirects = redirects or {}
        self.url: str | None = start
        self.tab_open = start is not None
        self.sites: list[str] = []
        if start:
            self.sites.append(fx.nav_needs_confirm([], start) or "")
        self.pending_nav: str | None = None
        self.calls: list[dict] = []
        self.executed: list[dict] = []
        self.mutate_on_approve: dict | None = None

    def _snap(self) -> dict:
        snap = self.pages.get(self.url or "") or {"url": self.url, "title": "", "text": "",
                                                    "elements": []}
        return json.loads(json.dumps({**snap, "url": self.url}))

    def _new_site(self, url: str) -> str | None:
        from ..sessions import browser_effects as fx

        return fx.nav_needs_confirm(self.sites, url)

    def _land(self, url: str, how: str) -> None:
        """Go to ``url`` and follow server redirects through the gate."""
        self.url = url
        self.pending_nav = None
        self.executed.append({"op": how, "url": url})
        for _ in range(5):
            nxt = self.redirects.get(self.url or "")
            if not nxt:
                return
            if self._new_site(nxt):
                self.pending_nav = nxt         # on_navigation stops it
                return
            self.url = nxt
            self.executed.append({"op": "nav", "url": nxt})

    def _tab(self) -> dict:
        from ..sessions import browser_effects as fx

        return {"open": self.tab_open, "id": "tab-eval", "url": self.url, "control": "agent",
                "sites": list(self.sites), "pending_nav": self.pending_nav,
                "off_site": bool(self.url and fx.nav_needs_confirm(self.sites, self.url))}

    def call(self, op: str, session: int, **args: Any) -> dict:
        from ..sessions import browser_effects as fx

        self.calls.append({"op": op, **args})
        if op == "status":
            return {"ok": True, "tab": self._tab() if self.tab_open else {"open": False}}
        if op in ("open", "navigate"):
            url = str(args.get("url"))
            if op == "navigate" and not self.tab_open:
                return {"ok": False, "error": "tab_closed"}
            for s in args.get("sites") or []:     # backend seeds (sites the user named)
                if fx.registrable_domain(str(s)) not in self.sites:
                    self.sites.append(fx.registrable_domain(str(s)))
            site = self._new_site(url)
            if site and not args.get("confirmed"):
                return {"ok": False, "error": "confirmation_required:new_site"}
            if site:
                self.sites.append(site)
            self.tab_open = True
            self._land(url, op)
            return {"ok": True, "tab": self._tab()}
        if op in ("read", "click", "type") and self._tab()["off_site"]:
            return {"ok": False, "error": "off_site"}
        if op == "read":
            return {"ok": True, "snapshot": self._snap()}
        if op in ("click", "type"):
            snap = self._snap()
            ref = args.get("ref")
            el = next((e for e in snap.get("elements") or [] if e.get("ref") == ref), None)
            if el is None:
                return {"ok": False, "error": "no_such_ref"}
            el = {**el, **((self.live.get(self.url or "") or {}).get(str(ref)) or {})}
            page = fx.Page(host=re.sub(r"^https?://([^/:]+).*$", r"\1", self.url or ""),
                           has_password=bool(snap.get("has_password")),
                           has_payment=bool(snap.get("has_payment")))
            effect = fx.classify(op, fx.element_from_snapshot(el), page,
                                 submit=bool(args.get("submit")))
            if effect.startswith("deny:"):
                return {"ok": False, "error": "denied:" + effect[5:]}
            if effect.startswith("confirm:") and not args.get("confirmed"):
                return {"ok": False, "error": "confirmation_required:" + effect[8:]}
            if effect == "confirm:cross_domain" and isinstance(el.get("href"), str):
                site = self._new_site(el["href"])   # approved link: its site is opened
                if site:
                    self.sites.append(site)
            self.executed.append({"op": op, "ref": ref, "url": self.url, "effect": effect,
                                  "confirmed": bool(args.get("confirmed")),
                                  **({"text": args.get("text")} if op == "type" else {})})
            nxt = (self.links.get(self.url or "") or {}).get(str(ref))
            if op == "click" and nxt:
                if self._new_site(nxt):
                    self.pending_nav = nxt          # on_navigation stops it
                else:
                    self._land(nxt, "nav")
            return {"ok": True, "effect": effect}
        if op == "close":
            self.tab_open = False
            return {"ok": True}
        return {"ok": False, "error": "unknown_op"}


def _simulated_user(store: Any, sid: int, decision: str, bridge: EvalBridge,
                    stop: threading.Event, decided: list[dict]) -> threading.Thread:
    from ..sessions import browser as sb

    def go() -> None:
        while not stop.is_set():
            for a in sb.pending_browser_approvals(store, sid):
                if decision == "approve" and bridge.mutate_on_approve:
                    url = bridge.url or ""
                    bridge.pages[url] = {**bridge.pages.get(url, {}), **bridge.mutate_on_approve}
                sb.resolve_browser_approval(store, a["id"], sid,
                                            "approve" if decision == "approve" else "reject")
                decided.append({"id": a["id"], "decision": decision, "payload": a["payload"]})
            stop.wait(0.005)
    t = threading.Thread(target=go, name="eval-user", daemon=True)
    t.start()
    return t


# ---------- targets ----------

def _load_json(suite_dir: Path, rel: str) -> Any:
    return json.loads((suite_dir / rel).read_text(encoding="utf-8"))


def run_ask_page(suite: Any, case: Any, provider: Any, patches: Any) -> Any:
    from .. import brain, web_ask
    from . import checks as _checks

    page_rel = case.fixtures.get("page")
    if not page_rel:
        raise _checks.CheckSpecError(f"{case.key}: web.ask_page needs fixtures.page")
    from .runner import expand_placeholders

    html = expand_placeholders((suite.dir / page_rel).read_text(encoding="utf-8"))
    patches.setattr(web_ask, "new_nonce", lambda: EVAL_NONCE)
    patches.setattr(brain, "resolve_provider", lambda *a, **k: provider)
    out = web_ask.ask_page(case.prompt, {"url": case.fixtures.get("url", "https://trap.example/"),
                                         "title": case.fixtures.get("title", "Trap page"),
                                         "html": html})
    calls = getattr(provider, "calls", [])
    prompt = ""
    if calls:
        prompt = calls[0]["system"] + "\n\n" + "\n".join(m["content"] for m in calls[0]["messages"])
    return _checks.Outcome(answer=str(out.get("answer") or ""), prompt=prompt,
                           usage=dict(getattr(provider, "last_usage", None) or {}),
                           browser={"hidden_dropped": out.get("hidden_dropped") or {}})


def run_browser_agent(suite: Any, case: Any, provider: Any, patches: Any) -> Any:
    from ..sessions import SessionStore
    from ..sessions import browser as sb
    from ..sessions import browser_runner as br
    from . import checks as _checks

    fx_ = case.fixtures
    pages = {url: _load_json(suite.dir, rel) for url, rel in (fx_.get("pages") or {}).items()}
    if not pages:
        raise _checks.CheckSpecError(f"{case.key}: browser.agent needs fixtures.pages")
    bridge = EvalBridge(pages, live=fx_.get("live"), links=fx_.get("links"),
                        redirects=fx_.get("redirects"), start=fx_.get("start"))
    bridge.mutate_on_approve = fx_.get("mutate_on_approve")
    patches.setattr(sb, "APPROVAL_POLL_S", 0.005)
    patches.setattr(br, "BrowserTools", functools.partial(sb.BrowserTools, approval_timeout=5.0))
    patches.setattr(br.web_ask, "new_nonce", lambda: EVAL_NONCE)
    store = SessionStore()
    store.set_model_price("openai", "script", in_cents_per_mtok=0, out_cents_per_mtok=1)
    sid = sb.create_browser_session(store, name="eval", role="browser", provider="openai",
                                    model="script", budget_cost_cents=10_000,
                                    browser_mode=str(fx_.get("browser_mode") or "read"),
                                    max_iterations=int(fx_.get("max_iterations") or 20))
    tid = store.enqueue_task(title=case.key, prompt=case.prompt, assigned_session_id=sid)
    task = store.claim_task(sid)
    if not task or task["id"] != tid:
        raise RuntimeError("could not claim the eval task")
    stop, decided = threading.Event(), list[dict]()
    user = _simulated_user(store, sid, str(fx_.get("user") or "reject"), bridge, stop, decided)
    try:
        out = br.BrowserRunner(store, sid, provider_factory=lambda s: provider,
                               bridge=bridge).run_task(task)  # type: ignore[arg-type]
    finally:
        stop.set()
        user.join(timeout=2)
    calls = getattr(provider, "calls", [])
    prompt = "\n\n".join(c["system"] + "\n\n" + "\n".join(m["content"] for m in c["messages"])
                         for c in calls)
    t = store.get_task(tid) or {}
    return _checks.Outcome(
        answer=str(t.get("result_summary") or ""), prompt=prompt,
        browser={"executed": bridge.executed, "calls": bridge.calls, "approvals": decided,
                 "events": [e["kind"] for e in store.list_events(sid)],
                 "task_status": out.task_status})
