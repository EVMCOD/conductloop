"""Supervisor: starts/stops session runners in threads (multi-agent PR 6,
D4/D8).

* ``start(session_id)`` — at most ``max_running`` sessions run at once
  (3 by default, configurable up to the hard maximum of 8, D4). Each session
  thread claims ONLY tasks the user assigned to it (D8; ``claim_task``
  enforces ``assigned_session_id``), runs them one at a time and heartbeats.
* ``stop(session_id)`` — clean cancellation: the runner stops between steps,
  gives the in-flight task back (``queued``, reservations released) and the
  session goes ``idle``. Worktree contents are never deleted.
* ``recover()`` — call once at process start. Sessions that were
  ``provisioning``/``running``/``closing`` when the previous process died have
  no live thread: they become ``stalled`` with an event and keep their task
  and reservations, so a human decides. They are NEVER relaunched
  automatically.
* ``check_heartbeats()`` — a running session whose heartbeat is older than
  ``stale_after_s`` is marked ``stalled`` (and asked to cancel).

No API/UI here; the process that hosts the dashboard owns one Supervisor.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import platform_caps
from . import limits, providers
from .runner import RunOutcome
from .store import SessionStore

log = logging.getLogger("conductloop.sessions.supervisor")

STARTABLE_STATUSES = ("created", "idle")
RESUMABLE_STATUSES = ("paused", "waiting_human", "stalled")
LIVE_STATUSES = ("provisioning", "running", "closing")


@dataclass
class _Handle:
    thread: threading.Thread
    cancel: threading.Event
    outcomes: list[RunOutcome] = field(default_factory=list)


class SupervisorError(RuntimeError):
    pass


class ResumeBlocked(SupervisorError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Supervisor:
    def __init__(self, store: SessionStore, *,
                 provider_factory: providers.ProviderFactory = providers.resolve_for_session,
                 max_running: int | None = None, poll_s: float = 1.0,
                 stale_after_s: int = 900, tools_kwargs: dict[str, Any] | None = None,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.provider_factory = provider_factory
        self.max_running = limits.resolve_max_active(max_running)
        self.poll_s, self.stale_after_s = poll_s, stale_after_s
        self.tools_kwargs = dict(tools_kwargs or {})
        self.tools_kwargs.pop("allow_unsandboxed", None)  # D2: never
        self._clock = clock
        self._lock = threading.Lock()
        self._handles: dict[int, _Handle] = {}

    # ── lifecycle ───────────────────────────────────────────────────────
    def running(self) -> list[int]:
        with self._lock:
            return [sid for sid, h in self._handles.items() if h.thread.is_alive()]

    def start(self, session_id: int, *, stop_when_idle: bool = False) -> None:
        s = self.store.get_session(session_id)
        if s is None:
            raise SupervisorError("no such session")
        if s["status"] not in STARTABLE_STATUSES:
            raise SupervisorError(f"session is {s['status']}; only {STARTABLE_STATUSES} can start")
        if not s["worktree_path"] and str(s.get("browser_mode") or "off") == "off":
            raise SupervisorError("session has no worktree")  # browser sessions (PR 8) have none
        if s["worktree_path"]:
            platform_caps.require_code_sessions()  # Windows: no sandbox, no code sessions
        with self._lock:
            alive = [sid for sid, h in self._handles.items() if h.thread.is_alive()]
            if session_id in alive:
                raise SupervisorError("session already running")
            if len(alive) >= self.max_running:
                raise limits.SessionLimitError(
                    f"{len(alive)} sessions running; limit is {self.max_running}")
            cancel = threading.Event()
            h = _Handle(threading.Thread(target=self._loop, args=(session_id, cancel, stop_when_idle),
                                         name=f"cl-session-{session_id}", daemon=True), cancel)
            self._handles[session_id] = h
        self.store.set_session_status(session_id, "idle")
        self.store.record_event(session_id, "session.started", {"max_running": self.max_running})
        h.thread.start()

    def resume(self, session_id: int, *, confirm_stalled: bool = False) -> None:
        """Explicit user action: continue a ``paused`` / ``waiting_human`` session
        (after raising the budget or setting a price) — or a ``stalled`` one only
        with ``confirm_stalled``. Refuses while the cause is still there."""
        s = self.store.get_session(session_id)
        if s is None:
            raise SupervisorError("no such session")
        if s["status"] not in RESUMABLE_STATUSES:
            raise SupervisorError(f"session is {s['status']}; only {RESUMABLE_STATUSES} resume")
        if s["status"] == "stalled" and not confirm_stalled:
            raise ResumeBlocked("confirm_required",
                                "stalled sessions only resume with an explicit confirmation")
        if s["provider"].lower() not in providers.FREE_PROVIDERS \
                and self.store.model_price(s["provider"], s["model"]) is None:
            raise ResumeBlocked("price_missing", "set a price for this model first")
        if s["cost_cents_est"] >= s["budget_cost_cents"]:
            raise ResumeBlocked("budget_exhausted", "raise the budget first")
        if s["iterations"] >= s["max_iterations"]:
            raise ResumeBlocked("iteration_limit", "raise max_iterations first")
        with self._lock:
            h = self._handles.get(session_id)
            if h is not None and h.thread.is_alive():
                raise SupervisorError("session thread still running")
        self.store.set_session_status(session_id, "idle")
        self.store.record_event(session_id, "session.resumed", {"from": s["status"]})
        self.start(session_id)

    def stop(self, session_id: int, *, timeout: float = 30.0) -> bool:
        """Cancel and wait. Returns True if the thread ended within ``timeout``."""
        with self._lock:
            h = self._handles.get(session_id)
        if h is None:
            return True
        h.cancel.set()
        h.thread.join(timeout)
        done = not h.thread.is_alive()
        if done:
            self.store.record_event(session_id, "session.stopped", {"by": "user"})
        return done

    def shutdown(self, timeout: float = 30.0) -> None:
        for sid in list(self._handles):
            self.stop(sid, timeout=timeout)

    def wait(self, session_id: int, timeout: float | None = None) -> list[RunOutcome]:
        with self._lock:
            h = self._handles.get(session_id)
        if h is None:
            return []
        h.thread.join(timeout)
        return list(h.outcomes)

    # ── the session thread ──────────────────────────────────────────────
    def _loop(self, session_id: int, cancel: threading.Event, stop_when_idle: bool) -> None:
        from .browser_runner import runner_class_for  # browser sessions: PR 8
        runner = runner_class_for(self.store, session_id)(
            self.store, session_id, provider_factory=self.provider_factory,
            cancel=cancel, tools_kwargs=self.tools_kwargs)
        h = self._handles[session_id]
        try:
            while not cancel.is_set():
                self.store.heartbeat(session_id)
                s = self.store.get_session(session_id)
                if s is None or s["status"] not in ("idle", "created"):
                    return  # paused / waiting_human / stalled / closed: a human acts
                # A task left in flight by a pause/stall the user resolved
                # (resume) continues first; otherwise claim the next assigned one.
                task = self.store.in_flight_task(session_id)
                if task is not None and task["status"] == "needs_review":
                    task = None
                if task is None:
                    task = self.store.claim_task(session_id)
                if task is None:
                    if stop_when_idle:
                        return
                    cancel.wait(self.poll_s)
                    continue
                outcome = runner.run_task(task)
                h.outcomes.append(outcome)
                if outcome.session_status != "idle":
                    return
        except Exception as e:  # never leave a session silently "running"
            log.exception("session %s crashed", session_id)
            self.store.set_session_status(session_id, "failed", last_error=f"supervisor: {e!s}"[:300])
            self.store.record_event(session_id, "session.crashed", {"error": type(e).__name__})

    # ── recovery / health ───────────────────────────────────────────────
    def recover(self) -> list[int]:
        """Mark sessions left live by a previous process as ``stalled``."""
        mine = set(self.running())
        out = []
        for s in self.store.list_sessions(active_only=True):
            if s["status"] in LIVE_STATUSES and s["id"] not in mine:
                self.store.set_session_status(s["id"], "stalled",
                                              last_error="process restarted while running; "
                                                         "not relaunched automatically")
                self.store.record_event(s["id"], "session.stalled", {"reason": "restart"})
                out.append(s["id"])
        return out

    def check_heartbeats(self) -> list[int]:
        now = int(self._clock())
        out = []
        for s in self.store.list_sessions(active_only=True):
            hb = s["heartbeat_at"] or s["updated_at"]
            if s["status"] in LIVE_STATUSES and now - int(hb or 0) > self.stale_after_s:
                with self._lock:
                    h = self._handles.get(s["id"])
                if h is not None:
                    h.cancel.set()
                self.store.set_session_status(s["id"], "stalled",
                                              last_error=f"no heartbeat for {now - int(hb or 0)}s")
                self.store.record_event(s["id"], "session.stalled", {"reason": "heartbeat"})
                out.append(s["id"])
        return out
