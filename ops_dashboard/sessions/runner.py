"""Per-session agent loop (multi-agent PR 6, D1/D4/D6).

``SessionRunner.run_task`` drives ONE claimed task with the session's own
provider + model (``providers.resolve_for_session``, never a fallback):

    model turn → (tool call → CodeTools → result back to the model)* → final

Protocol: provider-agnostic JSON in plain text (works with every
``brain.Provider``, including Ollama and mocks): the model answers with one
JSON object, ``{"tool": "<name>", "args": {...}}`` or
``{"final": "<summary>"}``. Tool failures (denied path, missing reservation,
no sandbox…) are returned to the model as ``{"ok": false, "error": …}``; they
never crash the loop.

Stops, always with an event and a visible status (never silently):
  * iteration cap (sessions/limits.py; 60 by default) → task ``blocked``,
    session ``waiting_human``;
  * budget (EUR, mandatory): warning event at 80 %; at 100 % — or when the
    next call is projected to cross it — session ``paused`` before spending;
  * no price configured for a paid provider → session ``waiting_human`` +
    a ``request`` message asking for the price, BEFORE any call;
  * provider errors / key in cooldown → ``waiting_human`` (no retry, no switch);
  * cancellation → task given back (``queued``, claims released), session ``idle``.

Finishing (D6): the files the agent wrote are committed on the session's own
``cl/`` branch (agent identity, local only), then ``worktrees.dry_merge``
against the base. Clean → task ``needs_review`` (a human integrates);
conflict → task ``blocked`` + ``conflict`` message. Never merges, never pushes.
"""
from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass
from typing import Any

from . import providers, sandbox, worktrees
from .code_tools import CodeTools, ToolDenied, ToolError
from .paths import PathConflictError
from .store import SessionStore

MAX_TOKENS_OUT = 1500
MAX_TOOL_RESULT_CHARS = 8000
KEEP_LAST_MESSAGES = 30
BUDGET_WARN_RATIO = 0.8

SYSTEM_PROMPT = """\
You are a coding agent working in a git worktree of the repository (your
own branch). Work only on the task you are given.

Reply with EXACTLY ONE JSON object and nothing else:
  {"tool": "<name>", "args": {...}}   to call a tool, or
  {"final": "<short summary of what you changed>"}   when the task is done.

Tools (paths are relative to the worktree root):
  list_dir   {"path": ".", "recursive": false}
  read_file  {"path": "...", "offset": 0}
  search     {"query": "...", "path": ".", "regex": false}
  claim      {"patterns": ["src/foo.py", "tests/*.py"]}   reserve files BEFORE writing
  write_file {"path": "...", "content": "..."}
  edit_file  {"path": "...", "old": "...", "new": "...", "count": 1}
  diff       {"against": "head"}
  run        {"argv": ["pytest", "-q", "tests/test_x.py"]}  only pytest, ruff check,
             ruff format --check, mypy, npm test / npm run lint; sandboxed, no network.
Refusals come back as {"ok": false, "error": "..."}: adapt, don't retry blindly.
You cannot commit, push, merge or install anything; a human reviews your work.
"""


def _skills_prompt(repo_path: str | None) -> str:
    """Global skills plus the repo's own (<repo>/.agents/skills, only if the
    user trusted the repo), offered through an activate_skill tool."""
    if not repo_path:
        return ""
    from ..skills import catalog as skills_catalog
    try:
        recs = skills_catalog.repo_catalog(repo_path)
    except Exception:  # noqa: BLE001 - a broken skills dir must not stop the session
        return ""
    if not recs:
        return ""
    cat = [{"name": r.key, "description": " ".join(r.description.split())[:skills_catalog.MAX_CATALOG_DESC]}
           for r in recs]
    return ('\nSkill tool:\n  activate_skill {"name": "..."}   load one of the skills below when it '
            "clearly fits the task" + skills_catalog.catalog_prompt(cat) + "\n")


@dataclass
class RunOutcome:
    task_status: str | None      # final task status, None if left in flight
    session_status: str
    reason: str
    iterations: int = 0
    commit: str | None = None
    conflicts: list[str] | None = None


def estimate_tokens(text: str) -> int:
    """Conservative estimate when a provider exposes no usage (~4 chars/token)."""
    return max(1, math.ceil(len(text) / 4))


def usage_tokens(usage: Any, prompt_tokens: int, reply: str) -> tuple[int, int, bool, str]:
    """(tokens_in, tokens_out, estimated, source). Real provider usage
    (``brain.extract_usage`` shape) wins; the ~4 chars/token estimate is only
    a fallback, per side, and marks the turn ``estimated``."""
    u = usage if isinstance(usage, dict) else {}
    tin, tout = u.get("input_tokens"), u.get("output_tokens")
    estimated = tin is None or tout is None or bool(u.get("split_estimated"))
    tin = int(tin) if tin is not None else prompt_tokens
    tout = int(tout) if tout is not None else estimate_tokens(reply)
    return tin, tout, estimated, str(u.get("source") or "estimate")


def parse_reply(text: str) -> dict | None:
    """First JSON object in the reply (tolerates ```json fences / prose)."""
    if not isinstance(text, str):
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    start = text.find("{")
    if start >= 0:
        candidates.append(text[start:])
    dec = json.JSONDecoder()
    for c in candidates:
        try:
            obj, _ = dec.raw_decode(c.strip())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


class SessionRunner:
    def __init__(self, store: SessionStore, session_id: int, *,
                 provider_factory: providers.ProviderFactory = providers.resolve_for_session,
                 cancel: threading.Event | None = None,
                 tools_kwargs: dict[str, Any] | None = None):
        self.store, self.session_id = store, int(session_id)
        self.provider_factory = provider_factory
        self.cancel = cancel or threading.Event()
        kw = dict(tools_kwargs or {})
        # D2: the runner NEVER runs commands without an OS sandbox.
        kw.pop("allow_unsandboxed", None)
        self.tools_kwargs = kw

    # ── helpers ─────────────────────────────────────────────────────────
    def _session(self) -> dict:
        s = self.store.get_session(self.session_id)
        if s is None:
            raise RuntimeError("session vanished")
        return s

    def _event(self, kind: str, payload: Any = None, *, task_id: int | None = None,
               **kw: Any) -> dict:
        return self.store.record_event(self.session_id, kind, payload, task_id=task_id, **kw)

    def _stop(self, task: dict, *, session_status: str, reason: str, task_status: str | None,
              iterations: int, summary: str | None = None, **extra: Any) -> RunOutcome:
        if task_status is not None:
            self.store.set_task_status(task["id"], self.session_id, task_status,
                                       result_summary=summary or reason)
        if self._session()["status"] == "stalled":
            session_status = "stalled"  # the supervisor flagged it; keep it visible
        else:
            self.store.set_session_status(self.session_id, session_status,
                                          last_error=None if session_status == "idle" else reason)
        self._event("session." + session_status, {"reason": reason, "task_status": task_status},
                    task_id=task["id"])
        return RunOutcome(task_status, session_status, reason, iterations, **extra)

    def _price_ok(self, s: dict, task: dict) -> bool:
        if s["provider"].lower() in providers.FREE_PROVIDERS:
            return True
        if self.store.model_price(s["provider"], s["model"]) is not None:
            return True
        if not self.store.has_event(self.session_id, "budget.price_missing"):
            self.store.post_message(
                kind="request", to_session=None, from_session=self.session_id, task_id=task["id"],
                body=(f"Session #{self.session_id} needs a price for {s['provider']}/{s['model']} "
                      "(EUR cents per million input/output tokens) before it can spend budget."))
        self._event("budget.price_missing", {"provider": s["provider"], "model": s["model"]},
                    task_id=task["id"])
        return False

    def _projected_cents(self, s: dict, prompt_tokens: int) -> float:
        if s["provider"].lower() in providers.FREE_PROVIDERS:
            return 0.0
        price = self.store.model_price(s["provider"], s["model"]) or {}
        return (prompt_tokens * float(price.get("in_cents_per_mtok", 0))
                + MAX_TOKENS_OUT * float(price.get("out_cents_per_mtok", 0))) / 1_000_000

    def _tool(self, tools: CodeTools, name: str, args: dict) -> dict:
        try:
            if name == "list_dir":
                return tools.list_dir(args.get("path", "."), recursive=bool(args.get("recursive")))
            if name == "read_file":
                return tools.read_file(args["path"], offset=int(args.get("offset", 0)))
            if name == "search":
                return tools.search(args["query"], args.get("path", "."),
                                    regex=bool(args.get("regex")))
            if name == "claim":
                return {"claims": [{"pattern": c["pattern"], "mode": c["mode"]}
                                   for c in tools.claim(list(args["patterns"]))]}
            if name == "write_file":
                r = tools.write_file(args["path"], args["content"])
                self._written.add(r["path"])
                return r
            if name == "edit_file":
                r = tools.edit_file(args["path"], args["old"], args["new"],
                                    count=int(args.get("count", 1)))
                self._written.add(r["path"])
                return r
            if name == "diff":
                return tools.diff(against=args.get("against", "head"))
            if name == "activate_skill":
                from ..skills import catalog as skills_catalog
                return skills_catalog.activate_for_repo(self._session()["repo_path"], args.get("name"))
            if name == "run":
                r = tools.run(list(args["argv"]), cwd=args.get("cwd", "."))
                self._written.update(r.get("changed_paths") or [])   # e.g. regenerated snapshots
                return r
            return {"ok": False, "error": f"unknown tool {name!r}"}
        except (ToolDenied, ToolError, PathConflictError, sandbox.SandboxUnavailable) as e:
            return {"ok": False, "error": str(e)[:500]}
        except (KeyError, TypeError, ValueError) as e:
            return {"ok": False, "error": f"bad arguments for {name}: {e!s}"[:500]}

    # ── main loop ───────────────────────────────────────────────────────
    def run_task(self, task: dict) -> RunOutcome:
        self._written: set[str] = set()
        tid = task["id"]
        resumed = task["status"] == "running"
        self.store.set_task_status(tid, self.session_id, "running")
        self.store.set_session_status(self.session_id, "running")
        self._event("task.started", {"title": task["title"]}, task_id=tid)
        s = self._session()
        try:
            tools = CodeTools(self.store, self.session_id, tid, **self.tools_kwargs)
        except (ToolDenied, worktrees.WorktreeError) as e:
            return self._stop(task, session_status="waiting_human", reason=f"worktree: {e}",
                              task_status="blocked", iterations=0)
        if resumed:
            # Edits from before the pause are still uncommitted in the worktree:
            # keep the ones this task may write so _finish commits them too.
            self._written.update(p for p in tools._dirty()
                                 if self.store.can_write(self.session_id, p, task_id=tid))
        system = SYSTEM_PROMPT + _skills_prompt(s["repo_path"])
        hints = json.loads(task.get("paths_hint") or "[]")
        messages: list[dict] = [{"role": "user", "content":
                                 f"Task #{tid}: {task['title']}\n\n{task['prompt']}"
                                 + (f"\n\nExpected paths: {', '.join(hints)}" if hints else "")
                                 + ("\n\n(Resumed after a pause: earlier work is already in the "
                                    "worktree; start with diff.)" if resumed else "")}]
        n = 0
        while True:
            if self.cancel.is_set():
                return self._stop(task, session_status="idle", reason="cancelled",
                                  task_status="queued", iterations=n)
            s = self._session()
            if s["iterations"] >= s["max_iterations"]:
                self._event("limit.iterations", {"max": s["max_iterations"]}, task_id=tid)
                # The task stays in flight so raising max_iterations + resume
                # picks it up again (a "blocked" task is never resumed).
                return self._stop(task, session_status="waiting_human",
                                  reason=f"iteration limit ({s['max_iterations']}) reached",
                                  task_status=None, iterations=n)
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
                messages.append({"role": "user", "content": "MESSAGES FROM THE USER:\n" + "\n".join(
                    f"- {m['body'][:2000]}" for m in inbox)})
            window = messages[:1] + messages[1:][-KEEP_LAST_MESSAGES:]
            prompt_text = system + "".join(m["content"] for m in window)
            prompt_tokens = estimate_tokens(prompt_text)
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
                        continue  # cancelled while waiting for the local model
                    reply = provider.ask(system, window, max_tokens=MAX_TOKENS_OUT,
                                         temperature=0.2)
            except providers.ProviderUnavailable as e:
                self._event("provider.unavailable", {"reason": e.reason}, task_id=tid)
                return self._stop(task, session_status="waiting_human", reason=str(e)[:300],
                                  task_status=None, iterations=n)
            except Exception as e:  # provider SDK errors: classified, never retried here
                code = getattr(e, "code", type(e).__name__)
                secs = providers.mark_failure(s, str(code))
                self._event("provider.error", {"code": str(code), "cooldown_s": secs},
                            task_id=tid)
                return self._stop(task, session_status="waiting_human",
                                  reason=f"provider error: {code}", task_status=None, iterations=n)
            if self.cancel.is_set():
                # Cancelled while the provider was answering: never act on it.
                return self._stop(task, session_status="idle", reason="cancelled",
                                  task_status="queued", iterations=n)
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
            s = self._session()
            if s["budget_tokens"] and s["tokens_in"] + s["tokens_out"] >= s["budget_tokens"]:
                self._event("budget.exhausted", {"tokens": s["tokens_in"] + s["tokens_out"],
                                                 "budget_tokens": s["budget_tokens"]}, task_id=tid)
                return self._stop(task, session_status="paused",
                                  reason="token budget exhausted", task_status=None, iterations=n)
            messages.append({"role": "assistant", "content": str(reply)[:MAX_TOOL_RESULT_CHARS]})
            if obj is None or not ("tool" in obj or "final" in obj):
                messages.append({"role": "user", "content":
                                 'TOOL_RESULT: {"ok": false, "error": "reply with one JSON object: '
                                 '{\\"tool\\": ..., \\"args\\": {...}} or {\\"final\\": ...}"}'})
                continue
            if "final" in obj:
                return self._finish(task, tools, str(obj.get("final") or ""), n)
            name, args = str(obj.get("tool")), obj.get("args") or {}
            result = self._tool(tools, name, args if isinstance(args, dict) else {})
            body = json.dumps(result, ensure_ascii=False, default=str)
            if len(body) > MAX_TOOL_RESULT_CHARS:
                body = body[:MAX_TOOL_RESULT_CHARS] + "… [truncated]"
            messages.append({"role": "user", "content": f"TOOL_RESULT {name}: {body}"})

    def _finish(self, task: dict, tools: CodeTools, summary: str, n: int) -> RunOutcome:
        s = self._session()
        tid = task["id"]
        current = self.store.get_task(tid)
        if self.cancel.is_set() or current is None or current["status"] != "running" \
                or current["owner_session_id"] != self.session_id:
            return self._stop(task, session_status="idle", reason="cancelled", task_status=None,
                              iterations=n)
        try:
            sha = worktrees.commit_paths(tools.root, sorted(self._written),
                                         f"cl: task #{tid} {task['title']}"[:200])
            branch = s["branch"]
            result = worktrees.dry_merge(s["repo_path"], s["base_ref"], branch)
        except worktrees.WorktreeError as e:
            return self._stop(task, session_status="waiting_human",
                              reason=f"integration check failed: {e}"[:300],
                              task_status="blocked", iterations=n)
        self._event("integration.dry_merge", {"clean": result.clean, "conflicts": result.conflicts,
                                              "commit": sha}, task_id=tid)
        if not result.clean:
            self.store.post_message(
                kind="conflict", from_session=self.session_id, to_session=None, task_id=tid,
                body=f"Task #{tid} conflicts with {s['base_ref']} in: "
                     + ", ".join(result.conflicts[:20]))
            return self._stop(task, session_status="idle", task_status="blocked",
                              reason="dry-merge conflict with base", iterations=n,
                              summary=f"{summary}\n\n[blocked: conflicts in "
                                      f"{', '.join(result.conflicts[:20])}]",
                              commit=sha, conflicts=result.conflicts)
        return self._stop(task, session_status="idle", task_status="needs_review",
                          reason="done; ready for human review", iterations=n,
                          summary=summary or "done", commit=sha, conflicts=[])
