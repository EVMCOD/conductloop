"""Agent loop for browser sessions (browser design PR 8; click/type PR 9).

Same contract as ``runner.SessionRunner`` (budget, iterations, price, provider
errors, cancellation, events — the helpers are inherited), different tools:
``BrowserTools`` instead of ``CodeTools``, and no commit at the end (a
browser task finishes in ``needs_review`` with the agent's summary).

Prompt injection (design §3.4): everything read from a page is wrapped in an
UNTRUSTED block with a per-task nonce and the rules are in the system prompt;
page text never grants permissions. Tools outside ``browser_mode`` are refused
by ``BrowserTools`` (ToolDenied-style result + ``browser.denied`` event).

Live view / takeover (§3.2): the tab is visible in the app. While the user has
paused or taken control, the runner sends no actions: it waits (heartbeating,
cancellable) and, when control comes back, tells the model to read again.
If the user closes the tab, the session goes ``waiting_human``.

Effects (§3.3, PR 9): in ``act`` mode ``click``/``type`` go through
``BrowserTools``, which asks the user (approval queue) for anything with an
effect and refuses passwords; the runner just waits, heartbeating.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from .. import web_ask
from . import providers
from .browser import BrowserBridge, BrowserDenied, BrowserTools, BrowserUnavailable, allowed_tools
from .runner import (
    BUDGET_WARN_RATIO,
    KEEP_LAST_MESSAGES,
    MAX_TOKENS_OUT,
    MAX_TOOL_RESULT_CHARS,
    RunOutcome,
    SessionRunner,
    estimate_tokens,
    parse_reply,
    usage_tokens,
)
from .store import SessionStore

CONTROL_POLL_S = 1.0


def bridge_factory() -> Any:
    """How runners reach the desktop app (patched in tests / embedding)."""
    return BrowserBridge()

BROWSER_SYSTEM = """\
You are a browsing agent working in a visible browser tab of the user's
ConductLoop app. Work only on the task you are given. {mode_line}

Reply with EXACTLY ONE JSON object and nothing else:
  {{"tool": "<name>", "args": {{...}}}}   to call a tool, or
  {{"final": "<answer / short summary of what you found>"}}   when done.

Tools:
{tools}
Refusals come back as {{"ok": false, "error": "..."}}: adapt, don't retry blindly.
Going to a website not opened in this session (by URL, link or redirect) waits
for the user's approval; stay on the sites of the task when you can.
If a page needs a login, a CAPTCHA or a password, say so in "final": the user
will take control. Never ask for or repeat passwords, keys or codes.
"""

MODE_LINES = {
    "read": "You can only read pages in this mode: you cannot click, type, log in, buy or post "
            "anything.",
    "act": "You can click and type on elements by their [ref] from your LAST read. Anything with "
           "an effect (submitting forms, buying, posting, sending, deleting, sensitive pages) "
           "waits for the user's approval; you never type passwords or card data. After every "
           "click/type, read the page again before the next action.",
}

TOOL_DOCS = {
    "open": '  open      {"url": "https://..."}   open your tab (first call) at a URL',
    "navigate": '  navigate  {"url": "https://..."}   go to another URL in your tab',
    "read": '  read      {}   read the current page: text + numbered interactive elements',
    "click": '  click     {"ref": 12}   click element [12] of your last read',
    "type": '  type      {"ref": 7, "text": "...", "submit": false}   replace the text of field [7]'
            ' (submit=true presses Enter)',
}


def system_prompt(mode: str, nonce: str) -> str:
    tools = "\n".join(TOOL_DOCS[t] for t in allowed_tools(mode) if t in TOOL_DOCS)
    return (BROWSER_SYSTEM.format(tools=tools, mode_line=MODE_LINES.get(mode, MODE_LINES["read"]))
            + web_ask.UNTRUSTED_RULES.format(nonce=nonce))


def _factory() -> Any:
    return bridge_factory()


class BrowserRunner(SessionRunner):
    def __init__(self, store: SessionStore, session_id: int, *,
                 provider_factory: providers.ProviderFactory = providers.resolve_for_session,
                 cancel: threading.Event | None = None,
                 tools_kwargs: dict[str, Any] | None = None,
                 bridge: BrowserBridge | None = None):
        super().__init__(store, session_id, provider_factory=provider_factory, cancel=cancel,
                         tools_kwargs=tools_kwargs)
        self.bridge = bridge or _factory()

    def _browser_tool(self, tools: BrowserTools, name: str, args: dict) -> dict:
        try:
            if name == "open":
                return tools.open(str(args.get("url") or ""))
            if name == "navigate":
                return tools.navigate(str(args.get("url") or ""))
            if name == "read":
                return tools.read()
            if name == "click":
                return tools.click(args.get("ref"))
            if name == "type":
                return tools.type(args.get("ref"), args.get("text"), bool(args.get("submit")))
            tools._check(name)  # any unknown name → denied
            return {"ok": False, "error": f"unknown tool {name!r}"}
        except BrowserDenied as e:
            return {"ok": False, "error": str(e)[:300]}

    def _wait_for_control(self, tools: BrowserTools, tid: int) -> str:
        """'agent' when the agent may act, 'returned' when it may act again
        after the user had control, 'closed' if the tab is gone, 'cancelled'.
        Waits (heartbeating, cancellable) while the user has control."""
        noted = False
        while not self.cancel.is_set():
            st = tools.status()
            raw = st.get("tab")
            tab: dict = raw if isinstance(raw, dict) else {}
            if not tab.get("open"):
                return "closed"
            if tab.get("control") == "agent":
                if noted:
                    self._event("browser.control", {"control": "agent"}, task_id=tid)
                    return "returned"
                return "agent"
            if not noted:
                self._event("browser.control", {"control": tab.get("control")}, task_id=tid)
                self.store.set_session_status(self.session_id, "running")
                noted = True
            self.store.heartbeat(self.session_id)
            self.cancel.wait(CONTROL_POLL_S)
        return "cancelled"

    def run_task(self, task: dict) -> RunOutcome:
        tid = task["id"]
        self.store.set_task_status(tid, self.session_id, "running")
        self.store.set_session_status(self.session_id, "running")
        self._event("task.started", {"title": task["title"]}, task_id=tid)
        tools = BrowserTools(self.store, self.session_id, bridge=self.bridge, cancel=self.cancel,
                             on_wait=lambda: self.store.heartbeat(self.session_id))
        tools.seed_sites(f"{task['title']}\n{task['prompt']}")   # sites the user named
        if not self.bridge.available:
            return self._stop(task, session_status="waiting_human", task_status="blocked",
                              reason="the in-app browser is not available (desktop app, macOS 14+)",
                              iterations=0)
        nonce = web_ask.new_nonce()
        messages: list[dict] = [{"role": "user", "content":
                                 f"Task #{tid}: {task['title']}\n\n{task['prompt']}"}]
        n = 0
        tab_opened = bool((tools.status().get("tab") or {}).get("open"))
        while True:
            if self.cancel.is_set():
                return self._stop(task, session_status="idle", reason="cancelled",
                                  task_status="queued", iterations=n)
            s = self._session()
            mode = str(s.get("browser_mode") or "off")
            if mode == "off":
                return self._stop(task, session_status="waiting_human", task_status="blocked",
                                  reason="browser_mode is off", iterations=n)
            if s["iterations"] >= s["max_iterations"]:
                self._event("limit.iterations", {"max": s["max_iterations"]}, task_id=tid)
                return self._stop(task, session_status="waiting_human",
                                  reason=f"iteration limit ({s['max_iterations']}) reached",
                                  task_status="blocked", iterations=n)
            if not self._price_ok(s, task):
                return self._stop(task, session_status="waiting_human",
                                  reason="no price configured for this model", task_status=None,
                                  iterations=n)
            wait = providers.cooldown_remaining(s)
            if wait:
                self._event("provider.cooldown", {"retry_after": wait}, task_id=tid)
                return self._stop(task, session_status="waiting_human",
                                  reason=f"key in cooldown for {wait}s", task_status=None,
                                  iterations=n)
            inbox = self.store.list_messages(self.session_id, unread_only=True)
            if inbox:
                self.store.mark_read(m["id"] for m in inbox)
                # Messages from other sessions are not the user: untrusted too.
                human = [m for m in inbox if m.get("from_session") is None]
                other = [m for m in inbox if m.get("from_session") is not None]
                for m in human:
                    tools.seed_sites(m["body"])
                if human:
                    messages.append({"role": "user", "content": "MESSAGES FROM THE USER:\n" + "\n".join(
                        f"- {m['body'][:2000]}" for m in human)})
                if other:
                    messages.append({"role": "user", "content": web_ask.wrap_untrusted(
                        "\n".join(f"- {m['body'][:2000]}" for m in other),
                        label="messages from other sessions", nonce=nonce)})
            system = system_prompt(mode, nonce)
            window = messages[:1] + messages[1:][-KEEP_LAST_MESSAGES:]
            prompt_tokens = estimate_tokens(system + "".join(m["content"] for m in window))
            spent, budget = float(s["cost_cents_est"]), float(s["budget_cost_cents"])
            if spent + self._projected_cents(s, prompt_tokens) > budget:
                self._event("budget.exhausted", {"spent_cents": spent, "budget_cents": budget,
                                                 "projected": True}, task_id=tid)
                return self._stop(task, session_status="paused",
                                  reason="budget exhausted (100%)", task_status=None, iterations=n)
            self.store.heartbeat(self.session_id)
            try:
                provider = self.provider_factory(s)
                with providers.provider_slot(s["provider"], self.cancel) as ok:
                    if not ok:
                        continue
                    reply = provider.ask(system, window, max_tokens=MAX_TOKENS_OUT, temperature=0.2)
            except providers.ProviderUnavailable as e:
                self._event("provider.unavailable", {"reason": e.reason}, task_id=tid)
                return self._stop(task, session_status="waiting_human", reason=str(e)[:300],
                                  task_status=None, iterations=n)
            except Exception as e:
                code = getattr(e, "code", type(e).__name__)
                secs = providers.mark_failure(s, str(code))
                self._event("provider.error", {"code": str(code), "cooldown_s": secs}, task_id=tid)
                return self._stop(task, session_status="waiting_human",
                                  reason=f"provider error: {code}", task_status=None, iterations=n)
            n += 1
            tin, tout, estimated, source = usage_tokens(getattr(provider, "last_usage", None),
                                                        prompt_tokens, str(reply))
            obj = parse_reply(reply)
            ev = self._event("llm.turn", {"estimated": estimated, "usage_source": source,
                                          "tool": (obj or {}).get("tool"),
                                          "final": bool(obj and "final" in obj)},
                             task_id=tid, tokens_in=tin, tokens_out=tout, iteration=True)
            ratio = ev["budget_ratio"]
            if ratio >= BUDGET_WARN_RATIO and not self.store.has_event(self.session_id,
                                                                       "budget.warning"):
                self._event("budget.warning", {"ratio": round(ratio, 3)}, task_id=tid)
            if ratio >= 1.0:
                self._event("budget.exhausted", {"ratio": round(ratio, 3)}, task_id=tid)
                return self._stop(task, session_status="paused",
                                  reason="budget exhausted (100%)", task_status=None, iterations=n)
            messages.append({"role": "assistant", "content": str(reply)[:MAX_TOOL_RESULT_CHARS]})
            if obj is None or not ("tool" in obj or "final" in obj):
                messages.append({"role": "user", "content":
                                 'TOOL_RESULT: {"ok": false, "error": "reply with one JSON object"}'})
                continue
            if "final" in obj:
                return self._stop(task, session_status="idle", task_status="needs_review",
                                  reason="done; ready for human review", iterations=n,
                                  summary=str(obj.get("final") or "done")[:4000])
            name, args = str(obj.get("tool")), obj.get("args") or {}
            args = args if isinstance(args, dict) else {}
            try:
                if tab_opened or name != "open":
                    state = self._wait_for_control(tools, tid)
                    if state == "cancelled":
                        continue
                    if state == "closed":
                        if tab_opened:
                            return self._stop(task, session_status="waiting_human",
                                              task_status=None, iterations=n,
                                              reason="the user closed the agent tab")
                        if name != "open":
                            messages.append({"role": "user", "content": 'TOOL_RESULT ' + name
                                             + ': {"ok": false, "error": "no tab yet: call open first"}'})
                            continue
                    elif state == "returned":
                        messages.append({"role": "user", "content":
                                         "NOTE: the user had control of the tab and gave it back; "
                                         "the page may have changed. Read it again before acting."})
                result = self._browser_tool(tools, name, args)
            except BrowserUnavailable as e:
                return self._stop(task, session_status="waiting_human", task_status=None,
                                  iterations=n, reason=str(e)[:300])
            if name == "open" and result.get("ok"):
                tab_opened = True
            if name == "read" and result.get("ok"):
                body = web_ask.wrap_untrusted(str(result["page"]), label="web page (read)",
                                              nonce=nonce)
                messages.append({"role": "user", "content": f"TOOL_RESULT read: {body}"})
                continue
            text = json.dumps(result, ensure_ascii=False, default=str)[:MAX_TOOL_RESULT_CHARS]
            messages.append({"role": "user", "content": f"TOOL_RESULT {name}: {text}"})


def runner_class_for(store: SessionStore, session_id: int) -> type[SessionRunner]:
    s = store.get_session(session_id) or {}
    return BrowserRunner if str(s.get("browser_mode") or "off") != "off" else SessionRunner
