"""Browse tool: team agents (chat, triggers, Telegram) delegate a task to a
visible browser session.

Built on the in-app browser (#103) and the agent tools (#114). The model never
gets a screenshot — only a text summary. Approvals for act-mode actions
(click / type / select) use the same queue as a browser session the user
started by hand. macOS only for now: elsewhere the tool returns a clear
``platform_unsupported`` error and costs nothing beyond the refusal.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

from . import platform_caps

log = logging.getLogger("conductloop.browse_tools")

TOOL_NAME = "browse"

# Modest defaults: a delegated browse is a side quest of a chat answer, not a
# long autonomous session. The user can open /browser and take over.
DEFAULT_BUDGET_CENTS = 50          # 0.50 EUR
DEFAULT_MAX_ITERATIONS = 10
DEFAULT_TIMEOUT_S = 90.0           # wall clock for the whole run
HARD_TIMEOUT_S = 300.0             # absolute ceiling (OPS_BROWSE_TIMEOUT)


def _timeout() -> float:
    raw = (os.environ.get("OPS_BROWSE_TIMEOUT") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return max(5.0, min(float(raw), HARD_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def tool_spec() -> dict[str, Any]:
    """One tool the Brain / Boardroom / team chat / wakes can call.

    Shape matches ``tools.TOOLS`` (Anthropic ``input_schema``); OpenAI wrapping
    happens in ``tools.tools_for_openai``.
    """
    return {
        "name": TOOL_NAME,
        "description": (
            "Delegate a browsing task to a visible browser tab in ConductLoop. "
            "Use it to look something up on the web, read a page, or (with "
            "mode=act) click/type/select under the user's approval. Returns a "
            "text summary only — never screenshots. macOS only; elsewhere it "
            "refuses with platform_unsupported. The user can watch the tab, "
            "pause it or take control at any time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "What to find or do in the browser (one clear goal).",
                },
                "start_url": {
                    "type": "string",
                    "description": "Optional https URL to open first.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["read", "act"],
                    "description": (
                        "read (default): open/navigate/read/back/scroll only. "
                        "act: also click/type/select; every select and anything "
                        "with an effect waits for the user's approval."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Short title for the Browser tab (optional).",
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    }


def available_tools() -> list[dict[str, Any]]:
    """Always offer the tool; on non-macOS a call refuses with platform_unsupported."""
    return [tool_spec()]


def prompt_block() -> str:
    """Added to the system prompt when the browse tool is offered."""
    where = ("available on this Mac" if platform_caps.browser_supported()
             else "NOT available on this computer (macOS 14+ only) — calling it "
                  "returns platform_unsupported")
    return (
        "\n\nBROWSER (tool `browse`):\n"
        f"- The in-app browser is {where}.\n"
        "- Call browse with a clear `task` (and optional `start_url`, `mode`) when "
        "the answer needs a live page. You get a text summary back, never a "
        "screenshot.\n"
        "- mode=read is the default. Use mode=act only when you must click, type "
        "or choose from a drop-down; those wait for the user's approval in the "
        "Browser tab (select always does).\n"
        "- The user can watch, pause or take over the tab while you wait.\n"
    )


def call(name: str, args: dict | None) -> dict[str, Any]:
    if name != TOOL_NAME:
        return {"ok": False, "error": f"unknown tool: {name}"}
    return browse(args or {})


def browse(args: dict[str, Any]) -> dict[str, Any]:
    """Run one browsing task and return a text summary (or a clear refusal)."""
    if not platform_caps.browser_supported():
        return {
            "ok": False,
            "error": "platform_unsupported",
            "message": platform_caps.browser_message(),
        }

    task = (args.get("task") if isinstance(args.get("task"), str) else "") or ""
    task = task.strip()
    if not task:
        return {"ok": False, "error": "bad_request",
                "message": "browse needs a non-empty `task`."}

    start_url = args.get("start_url")
    if start_url is not None and not isinstance(start_url, str):
        return {"ok": False, "error": "bad_request", "message": "start_url must be a string."}
    start_url = (start_url or "").strip() or None
    if start_url and not (start_url.startswith("https://") or start_url.startswith("http://")):
        return {"ok": False, "error": "bad_request",
                "message": "start_url must be an http(s) URL."}

    mode = (args.get("mode") if isinstance(args.get("mode"), str) else "read") or "read"
    mode = mode.strip().lower()
    if mode not in ("read", "act"):
        return {"ok": False, "error": "bad_request", "message": "mode must be 'read' or 'act'."}

    title = args.get("title") if isinstance(args.get("title"), str) else None
    title = ((title or "").strip() or task[:80])[:200]

    try:
        return _run_delegated(task=task, title=title, start_url=start_url, mode=mode)
    except Exception as e:
        log.exception("browse delegate failed")
        return {"ok": False, "error": "internal_error",
                "message": f"the browser agent did not finish: {type(e).__name__}"}


def _run_delegated(*, task: str, title: str, start_url: str | None,
                   mode: str) -> dict[str, Any]:
    from . import brain
    from .sessions import SessionStore
    from .sessions import browser as sb
    from .sessions.browser_runner import BrowserRunner

    # Prefer the user's default BYOK provider; a missing key is a clear refusal.
    try:
        prov = brain.resolve_provider(None)
    except Exception as e:
        return {"ok": False, "error": "no_provider",
                "message": f"no model provider available to drive the browser: {e}"}
    if not getattr(prov, "available", lambda: False)():
        return {"ok": False, "error": "no_provider",
                "message": "no model provider available to drive the browser. "
                           "Put a key in Credentials first."}
    # The mock provider is a fallback for chat, not a driver for a live tab.
    if getattr(prov, "name", None) == "mock" and not os.environ.get("OPS_BROWSE_ALLOW_MOCK"):
        return {"ok": False, "error": "no_provider",
                "message": "no real model provider available to drive the browser. "
                           "Put a key in Credentials first."}

    provider_name = getattr(prov, "name", None) or "openai"
    model = getattr(prov, "model", None) or getattr(prov, "default_model", None) or "default"

    store = SessionStore()
    try:
        sid = sb.create_browser_session(
            store,
            name=f"Delegated: {title[:40]}",
            role="browser",
            provider=str(provider_name),
            model=str(model),
            budget_cost_cents=DEFAULT_BUDGET_CENTS,
            browser_mode=mode,
            max_iterations=DEFAULT_MAX_ITERATIONS,
            key_ref=None,
        )
    except Exception as e:
        return {"ok": False, "error": "session_refused",
                "message": f"could not open a browser session: {e}"}

    prompt = task if not start_url else f"Start at {start_url}\n\n{task}"
    tid = store.enqueue_task(title=title, prompt=prompt, assigned_session_id=sid,
                             created_by="browse_delegate")
    claimed = store.claim_task(sid, tid)
    if claimed is None:
        claimed = store.get_task(tid) or {"id": tid, "title": title, "prompt": prompt}

    cancel = threading.Event()
    outcome_box: dict[str, Any] = {}

    def _provider_factory(session: dict):
        # Always the same resolved provider for this delegation.
        return prov

    def _worker() -> None:
        try:
            runner = BrowserRunner(store, sid, provider_factory=_provider_factory,
                                   cancel=cancel)
            outcome_box["outcome"] = runner.run_task(claimed)
        except Exception as e:
            outcome_box["error"] = e

    th = threading.Thread(target=_worker, name=f"browse-delegate-{sid}", daemon=True)
    th.start()
    th.join(timeout=_timeout())
    if th.is_alive():
        cancel.set()
        th.join(timeout=5.0)
        # Leave the session for the user to inspect; do not delete it.
        store.set_session_status(sid, "waiting_human")
        return {
            "ok": False,
            "error": "timeout",
            "message": (f"the browsing task did not finish within {_timeout():.0f}s. "
                        f"Open Browser (session #{sid}) to watch or take over."),
            "session_id": sid,
            "task_id": tid,
        }

    if "error" in outcome_box:
        err = outcome_box["error"]
        return {"ok": False, "error": "runner_failed",
                "message": f"the browser agent failed: {type(err).__name__}: {err}",
                "session_id": sid, "task_id": tid}

    outcome = outcome_box.get("outcome")
    task_status = getattr(outcome, "task_status", None)
    reason = getattr(outcome, "reason", None)
    if task_status is None and isinstance(outcome, dict):
        task_status = outcome.get("task_status")
        reason = outcome.get("reason")
    summary = _summary_from(store, tid, outcome)
    ok = task_status in ("needs_review", "done")
    out: dict[str, Any] = {
        "ok": ok,
        "session_id": sid,
        "task_id": tid,
        "mode": mode,
        "summary": summary,
        "task_status": task_status,
    }
    if not ok:
        out["error"] = "not_finished"
        out["message"] = reason or "the browsing task did not finish"
    # Never attach screenshots or DOM dumps — summary is text only.
    return out


def _summary_from(store: Any, task_id: int, outcome: Any) -> str:
    """Prefer the task's result_summary; fall back to the runner's reason."""
    try:
        row = store.get_task(task_id) or {}
        for key in ("result_summary", "output_summary", "summary"):
            val = row.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:4000]
    except Exception:
        pass
    for attr in ("reason", "summary", "final", "answer"):
        val = getattr(outcome, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()[:4000]
    if isinstance(outcome, dict):
        for key in ("reason", "summary", "final", "answer"):
            val = outcome.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:4000]
    return "(no summary — open the Browser tab for what happened)"
