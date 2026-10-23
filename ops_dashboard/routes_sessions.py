"""Multi-agent sessions API (multi-agent PR 7). No UI here (PR 8).

Registered on the shared route registry (``routing.router``); importing this
module registers the routes (``routes_meta`` imports it, so server.py is
untouched).

Protection — the SAME gate every local write route uses today (server.py):
  * GET goes through ``do_GET`` → ``_get_allowed``: loopback is trusted;
    anything else needs a paired-device Bearer token or a session cookie.
  * POST goes through ``do_POST``'s gate (none of these paths is in
    ``_PUBLIC_POSTS``): loopback trusted; otherwise authentication AND the
    double-submit CSRF token (Bearer clients are exempt from CSRF).
  * Extra, only for these routes (defence in depth, because loopback trust +
    the server's permissive CORS would otherwise let any web page open in the
    user's browser drive this API): requests carrying a cross-site
    ``Origin`` / ``Sec-Fetch-Site`` are refused with 403 ``forbidden_origin``.

Responses never contain key values (sessions only store ``key_ref``) and every
JSON body is passed through ``limits.redact`` as a last line of defence.
Errors are stable JSON: ``{"ok": false, "error": <code>, "message": …}``;
``provider_unavailable`` uses the same shape as #15.
"""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from .routing import router
from .sessions import limits, providers, worktrees
from .sessions.limits import SessionLimitError, SessionValidationError
from .sessions.paths import PathConflictError
from .sessions.store import SessionStore
from .sessions.supervisor import ResumeBlocked, Supervisor, SupervisorError

KNOWN_PROVIDERS = ("anthropic", "openai", "minimax", "gateway", "ollama", "mock")
KEYED_PROVIDERS = ("anthropic", "openai", "minimax", "gateway")   # key_ref required
MAX_BODY = 1024 * 1024
MAX_DIFF_BYTES = 256 * 1024

# ── process-wide supervisor ─────────────────────────────────────────────────

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"sup": None, "factory": None, "tools_kwargs": None}


def configure(*, provider_factory: providers.ProviderFactory | None = None,
              tools_kwargs: dict | None = None) -> None:
    """Replace the supervisor (tests / embedding). Stops the current one."""
    with _LOCK:
        old = _STATE["sup"]
        _STATE.update(sup=None, factory=provider_factory, tools_kwargs=tools_kwargs)
    if old is not None:
        old.shutdown(timeout=10)


def supervisor() -> Supervisor:
    with _LOCK:
        if _STATE["sup"] is None:
            sup = Supervisor(SessionStore(),
                             provider_factory=_STATE["factory"] or providers.resolve_for_session,
                             tools_kwargs=_STATE["tools_kwargs"])
            sup.recover()  # sessions left live by a previous process → stalled
            _STATE["sup"] = sup
        return _STATE["sup"]


def _store() -> SessionStore:
    return supervisor().store


# ── plumbing ────────────────────────────────────────────────────────────────

class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def _send(handler, status: int, obj: Any) -> None:
    body = limits.redact(json.dumps(obj, default=str, ensure_ascii=False))
    handler._send(status, body.encode("utf-8"), "application/json")


def _error(handler, e: ApiError) -> None:
    _send(handler, e.status, {"ok": False, "error": e.code, "code": e.code,
                              "message": e.message, **e.extra})


def _origin_ok(handler) -> bool:
    if (handler.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
        return False
    origin = handler.headers.get("Origin")
    if origin is None:
        return True
    if origin == "null":
        return False
    host = (handler.headers.get("Host") or "").lower()
    return bool(host) and urlparse(origin).netloc.lower() == host


def _body(handler) -> dict:
    try:
        n = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        raise ApiError(400, "bad_request", "invalid Content-Length") from None
    if n > MAX_BODY:
        raise ApiError(413, "payload_too_large", "request body too large")
    raw = handler.rfile.read(n) if n else b""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise ApiError(400, "invalid_json", "body must be a JSON object") from None
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "body must be a JSON object")
    return data


def _qs(handler) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(handler.path).query).items()}


def _int(value: Any, field: str, *, minimum: int | None = None, maximum: int | None = None,
         default: int | None = None, from_query: bool = False) -> int:
    if value is None or value == "":
        if default is not None:
            return default
        raise ApiError(400, "validation_error", f"{field}: required", field=field)
    try:
        if isinstance(value, bool) or (isinstance(value, str) and not from_query):
            raise ValueError  # JSON bodies must carry real numbers
        out = int(value)
        if isinstance(value, float) and value != out:
            raise ValueError
    except (TypeError, ValueError):
        raise ApiError(400, "validation_error", f"{field}: must be an integer",
                       field=field) from None
    if (minimum is not None and out < minimum) or (maximum is not None and out > maximum):
        raise ApiError(400, "validation_error", f"{field}: out of range", field=field)
    return out


def _str(data: dict, field: str, *, required: bool = True, max_len: int = 500) -> str | None:
    v = data.get(field)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ApiError(400, "validation_error", f"{field}: required", field=field)
        return None
    if not isinstance(v, str) or len(v) > max_len:
        raise ApiError(400, "validation_error", f"{field}: must be a string (max {max_len})",
                       field=field)
    return v.strip()


def _page(handler) -> tuple[int, int]:
    q = _qs(handler)
    return (_int(q.get("after_id"), "after_id", minimum=0, default=0, from_query=True),
            _int(q.get("limit"), "limit", minimum=1, maximum=500, default=100, from_query=True))


def _paged(items: list[dict], after_id: int) -> dict:
    return {"ok": True, "items": items,
            "next_after_id": items[-1]["id"] if items else after_id}


def route(method: str, path: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        def wrapped(handler, **kw: str) -> None:
            try:
                if not _origin_ok(handler):
                    raise ApiError(403, "forbidden_origin", "cross-site requests are not allowed")
                status, obj = fn(handler, **kw)
                _send(handler, status, obj)
            except ApiError as e:
                _error(handler, e)
            except SessionValidationError as e:
                # SessionValidationError messages never echo the submitted value.
                _error(handler, ApiError(400, "validation_error", str(e), field=e.field))
            except SessionLimitError as e:
                _error(handler, ApiError(409, "limit_reached", str(e)))
            except PathConflictError as e:
                _error(handler, ApiError(409, "path_conflict", str(e), conflicts=e.conflicts))
            except ResumeBlocked as e:
                _error(handler, ApiError(409, e.code, str(e)))
            except SupervisorError as e:
                _error(handler, ApiError(409, "invalid_state", str(e)))
            except worktrees.WorktreeError as e:
                _error(handler, ApiError(400, "worktree_error", str(e)))
            except providers.ProviderUnavailable as e:
                _error(handler, ApiError(503, "provider_unavailable", str(e),
                                         provider=e.provider, model=None,
                                         retry_after=e.retry_after, reason=e.reason))
            except Exception as e:  # pragma: no cover - generic 500, no details leaked
                handler._send_500(e, "sessions api")
        router.add(method, path, wrapped)
        return wrapped
    return deco


def _session_or_404(sid: str) -> dict:
    try:
        n = int(sid)
    except ValueError:
        raise ApiError(404, "not_found", "no such session") from None
    s = _store().get_session(n)
    if s is None:
        raise ApiError(404, "not_found", "no such session")
    return s


def _task_or_404(tid: str) -> dict:
    try:
        n = int(tid)
    except ValueError:
        raise ApiError(404, "not_found", "no such task") from None
    t = _store().get_task(n)
    if t is None:
        raise ApiError(404, "not_found", "no such task")
    return t


def _preflight_provider(s: dict) -> None:
    """Resolve the session's provider (explicit, no fallback) before starting or
    resuming, so a missing key / provider in cooldown fails fast as
    ``provider_unavailable`` (503) instead of inside the thread."""
    supervisor().provider_factory(s)


def _public_session(s: dict) -> dict:
    out = dict(s)
    out["running"] = s["id"] in supervisor().running()
    out["budget_eur"] = round(s["budget_cost_cents"] / 100, 2)
    out["spent_eur_est"] = round(float(s["cost_cents_est"]) / 100, 4)
    for k in ("scope_globs",):
        try:
            out[k] = json.loads(out.get(k) or "[]")
        except ValueError:
            pass
    return out


def _public_task(t: dict) -> dict:
    out = dict(t)
    for k in ("paths_hint", "depends_on", "commits"):
        try:
            out[k] = json.loads(out.get(k) or "[]")
        except ValueError:
            pass
    return out


def _decode_payload(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        r = dict(r)
        if isinstance(r.get("payload"), str):
            try:
                r["payload"] = json.loads(r["payload"])
            except ValueError:
                pass
        out.append(r)
    return out


# ── repos ───────────────────────────────────────────────────────────────────

@route("GET", "/api/sessions/repos")
def list_repos(handler):
    return 200, {"ok": True, "items": _store().list_repos()}


@route("POST", "/api/sessions/repos")
def mark_repo(handler):
    data = _body(handler)
    path = _str(data, "path", max_len=4096) or ""
    p = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(p):
        raise ApiError(400, "validation_error", "path: directory not found", field="path")
    try:
        top = str(worktrees.repo_toplevel(p))
    except worktrees.WorktreeError:
        raise ApiError(400, "not_a_repo", "path is not a git repository", field="path") from None
    if os.path.realpath(top) != p:
        raise ApiError(400, "not_a_repo", f"path must be the repository root ({top})", field="path")
    rp = _store().mark_repo(top, project_key=_str(data, "project_key", required=False, max_len=100))
    return 201, {"ok": True, "repo_path": rp}


@route("POST", "/api/sessions/repos/unmark")
def unmark_repo(handler):
    data = _body(handler)
    path = _str(data, "path", max_len=4096) or ""
    st = _store()
    rp = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    known = {r["repo_path"] for r in st.list_repos()}
    if rp not in known:
        rp = os.path.realpath(rp)
        if rp not in known:
            raise ApiError(404, "not_found", "repo is not marked")
    if st.active_sessions_on_repo(rp):
        raise ApiError(409, "invalid_state", "close the repo's active sessions first")
    st.mark_repo(rp, enabled=False)
    return 200, {"ok": True, "repo_path": rp, "enabled": False}


# ── prices ──────────────────────────────────────────────────────────────────

@route("GET", "/api/sessions/prices")
def list_prices(handler):
    return 200, {"ok": True, "items": _store().list_model_prices(), "currency": "EUR"}


@route("POST", "/api/sessions/prices")
def set_price(handler):
    data = _body(handler)
    provider = (_str(data, "provider", max_len=40) or "").lower()
    if provider not in KNOWN_PROVIDERS:
        raise ApiError(400, "validation_error", "provider: unknown", field="provider")
    model = _str(data, "model", max_len=200) or ""
    vals = {}
    for f in ("in_cents_per_mtok", "out_cents_per_mtok"):
        v = data.get(f)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0 or v > 10_000_000:
            raise ApiError(400, "validation_error", f"{f}: must be a number >= 0", field=f)
        vals[f] = float(v)
    _store().set_model_price(provider, model, currency=str(data.get("currency") or "EUR"), **vals)
    return 200, {"ok": True, "price": _store().model_price(provider, model)}


# ── sessions ────────────────────────────────────────────────────────────────

@route("GET", "/api/sessions")
def list_sessions(handler):
    active = _qs(handler).get("active") in ("1", "true")
    return 200, {"ok": True, "items": [_public_session(s)
                                       for s in _store().list_sessions(active_only=active)],
                 "max_running": supervisor().max_running}


@route("POST", "/api/sessions")
def create_session(handler):
    data = _body(handler)
    role = _str(data, "role", max_len=60) or ""
    provider = (_str(data, "provider", max_len=40) or "").lower()
    if provider not in KNOWN_PROVIDERS:
        raise ApiError(400, "validation_error", "provider: unknown", field="provider")
    model = _str(data, "model", max_len=200) or ""
    repo = _str(data, "repo_path", max_len=4096) or ""
    base_ref = _str(data, "base_ref", max_len=200) or ""
    if base_ref.startswith("-"):
        raise ApiError(400, "validation_error", "base_ref: invalid", field="base_ref")
    key_ref = _str(data, "key_ref", required=provider in KEYED_PROVIDERS, max_len=200)
    budget = _int(data.get("budget_cost_cents"), "budget_cost_cents", minimum=1)
    kwargs: dict[str, Any] = {}
    if data.get("max_iterations") is not None:
        kwargs["max_iterations"] = _int(data["max_iterations"], "max_iterations")
    scope = data.get("scope_globs") or []
    if not isinstance(scope, list) or not all(isinstance(x, str) for x in scope):
        raise ApiError(400, "validation_error", "scope_globs: list of strings", field="scope_globs")
    st = _store()
    sid = st.create_session(name=_str(data, "name", required=False, max_len=80) or role,
                            role=role, provider=provider, model=model, repo_path=repo,
                            base_ref=base_ref, budget_cost_cents=budget, key_ref=key_ref,
                            scope_globs=scope, **kwargs)
    try:
        info = worktrees.create(st.get_session(sid)["repo_path"], base_ref,  # type: ignore[index]
                                session_id=sid, role=role)
    except worktrees.WorktreeError as e:
        st.set_session_status(sid, "failed", last_error=f"worktree: {e}"[:300])
        raise ApiError(400, "worktree_error", str(e), session_id=sid) from None
    st.set_session_worktree(sid, info.path, info.branch)
    st.record_event(sid, "session.created", {"provider": provider, "model": model,
                                             "branch": info.branch})
    return 201, {"ok": True, "session": _public_session(st.get_session(sid) or {})}


@route("GET", "/api/sessions/inbox")
def inbox(handler):
    after, limit = _page(handler)
    return 200, _paged(_store().list_session_messages(None, after_id=after, limit=limit), after)


# tasks & claims are registered before /api/sessions/<sid> so the literal
# segments win the first-match dispatch.

@route("GET", "/api/sessions/tasks")
def list_tasks(handler):
    q = _qs(handler)
    after, limit = _page(handler)
    sid = _int(q["session_id"], "session_id", minimum=1, from_query=True) if q.get("session_id") else None
    status = q.get("status")
    if status is not None and status not in limits.TASK_STATUSES:
        raise ApiError(400, "validation_error", "status: unknown", field="status")
    items = _store().list_tasks(session_id=sid, status=status, after_id=after, limit=limit)
    return 200, _paged([_public_task(t) for t in items], after)


@route("POST", "/api/sessions/tasks")
def create_task(handler):
    data = _body(handler)
    sid = _int(data.get("session_id"), "session_id", minimum=1)
    s = _store().get_session(sid)
    if s is None or s["status"] not in limits.ACTIVE_SESSION_STATUSES:
        raise ApiError(400, "validation_error", "session_id: no such active session",
                       field="session_id")
    hints = data.get("paths_hint") or []
    deps = data.get("depends_on") or []
    if not isinstance(hints, list) or not all(isinstance(h, str) for h in hints):
        raise ApiError(400, "validation_error", "paths_hint: list of strings", field="paths_hint")
    if not isinstance(deps, list) or not all(isinstance(d, int) and not isinstance(d, bool)
                                             for d in deps):
        raise ApiError(400, "validation_error", "depends_on: list of task ids", field="depends_on")
    tid = _store().enqueue_task(title=_str(data, "title", max_len=200) or "",
                                prompt=_str(data, "prompt", required=False, max_len=20_000) or "",
                                assigned_session_id=sid, paths_hint=hints, depends_on=deps,
                                priority=_int(data.get("priority"), "priority", minimum=-100,
                                              maximum=100, default=0))
    return 201, {"ok": True, "task": _public_task(_store().get_task(tid) or {})}


@route("POST", "/api/sessions/tasks/<tid>/cancel")
def cancel_task(handler, tid):
    t = _task_or_404(tid)
    if t["status"] in ("done", "cancelled"):
        raise ApiError(409, "invalid_state", f"task is already {t['status']}")
    owner = t["owner_session_id"]
    sup = supervisor()
    stopped = True
    if owner and owner in sup.running():
        stopped = sup.stop(owner)    # the runner gives the task back cleanly
    # Cancelling is final even if the runner is still inside a slow provider
    # call: it re-checks the task before acting and cannot change its state.
    if not _store().cancel_task(t["id"]):
        raise ApiError(409, "invalid_state", "task could not be cancelled")
    return 200, {"ok": True, "runner_stopped": stopped,
                 "task": _public_task(_store().get_task(t["id"]) or {})}


@route("POST", "/api/sessions/tasks/<tid>/review")
def review_task(handler, tid):
    t = _task_or_404(tid)
    data = _body(handler)
    decision = data.get("decision")
    if decision not in ("accept", "reject"):
        raise ApiError(400, "validation_error", "decision: 'accept' or 'reject'", field="decision")
    if t["status"] != "needs_review":
        raise ApiError(409, "invalid_state", f"task is {t['status']}, not needs_review")
    _store().review_task(t["id"], accept=decision == "accept",
                         note=_str(data, "note", required=False, max_len=2000))
    if t["owner_session_id"]:
        _store().record_event(t["owner_session_id"], "task.reviewed",
                              {"decision": decision}, task_id=t["id"])
    # Accepting never merges: the branch/diff stay for the human to integrate.
    return 200, {"ok": True, "merged": False, "task": _public_task(_store().get_task(t["id"]) or {})}


@route("GET", "/api/sessions/claims")
def list_claims(handler):
    q = _qs(handler)
    sid = _int(q["session_id"], "session_id", minimum=1, from_query=True) if q.get("session_id") else None
    return 200, {"ok": True, "items": _store().active_claims(session_id=sid)}


@route("POST", "/api/sessions/claims/release")
def release_claims(handler):
    data = _body(handler)
    sid = _int(data.get("session_id"), "session_id", minimum=1)
    ids = data.get("claim_ids")
    if ids is not None and (not isinstance(ids, list) or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        raise ApiError(400, "validation_error", "claim_ids: list of ids", field="claim_ids")
    n = _store().release_paths(sid, claim_ids=ids)
    return 200, {"ok": True, "released": n}


@route("GET", "/api/sessions/<sid>")
def session_detail(handler, sid):
    s = _session_or_404(sid)
    st = _store()
    wt: dict[str, Any] | None = None
    if s["worktree_path"] and s["status"] not in ("closed", "failed"):
        try:
            info = worktrees.inspect(s["repo_path"], s["worktree_path"], s["base_ref"])
            wt = {"path": info.path, "branch": info.branch, "dirty": info.dirty,
                  "changed": info.changed, "ahead": info.ahead,
                  "files_vs_base": info.files_vs_base}
        except worktrees.WorktreeError as e:
            wt = {"error": str(e)}
    task = st.in_flight_task(s["id"])
    return 200, {"ok": True, "session": _public_session(s),
                 "task": _public_task(task) if task else None,
                 "claims": st.active_claims(session_id=s["id"]), "worktree": wt}


@route("POST", "/api/sessions/<sid>/start")
def start_session(handler, sid):
    s = _session_or_404(sid)
    _preflight_provider(s)
    supervisor().start(s["id"])
    return 200, {"ok": True, "session": _public_session(_store().get_session(s["id"]) or {})}


@route("POST", "/api/sessions/<sid>/stop")
def stop_session(handler, sid):
    s = _session_or_404(sid)
    stopped = supervisor().stop(s["id"])
    return 200, {"ok": True, "stopped": stopped,
                 "session": _public_session(_store().get_session(s["id"]) or {})}


@route("POST", "/api/sessions/<sid>/close")
def close_session(handler, sid):
    s = _session_or_404(sid)
    if s["status"] in ("closed", "failed"):
        raise ApiError(409, "invalid_state", f"session is already {s['status']}")
    sup = supervisor()
    if not sup.stop(s["id"]):
        raise ApiError(409, "invalid_state", "session did not stop in time; retry")
    st = _store()
    t = st.in_flight_task(s["id"])
    if t is not None and t["status"] in ("claimed", "running"):
        st.set_task_status(t["id"], s["id"], "queued",
                           result_summary="returned to the queue: session closed")
    st.set_session_status(s["id"], "closed")
    st.record_event(s["id"], "session.closed", {"by": "user"})
    # Prune the worktree when it is clean and has no commits to review.
    # Without this, every session left a full clone and a cl/<role>/<id>
    # branch behind, forever (worktrees.prune had no caller at all).
    worktree_kept = s["worktree_path"]
    pruned = None
    if s.get("worktree_path") and s.get("repo_path"):
        try:
            from ..sessions import worktrees as _wt
            res = _wt.prune(s["repo_path"], s["worktree_path"],
                            s.get("base_ref") or "HEAD")
            pruned = res.reason
            if res.removed:
                worktree_kept = None
        except Exception as e:
            pruned = f"kept: {e}"
    return 200, {"ok": True, "session": _public_session(st.get_session(s["id"]) or {}),
                 "worktree_kept": worktree_kept, "worktree_prune": pruned}


@route("POST", "/api/sessions/<sid>/resume")
def resume_session(handler, sid):
    s = _session_or_404(sid)
    data = _body(handler)
    budget = data.get("budget_cost_cents")
    iters = data.get("max_iterations")
    if budget is not None or iters is not None:
        _store().update_session_limits(
            s["id"],
            budget_cost_cents=_int(budget, "budget_cost_cents", minimum=1) if budget is not None else None,
            max_iterations=_int(iters, "max_iterations", minimum=1) if iters is not None else None)
    confirm = data.get("confirm_stalled") is True
    _preflight_provider(s)
    supervisor().resume(s["id"], confirm_stalled=confirm)
    return 200, {"ok": True, "session": _public_session(_store().get_session(s["id"]) or {})}


@route("GET", "/api/sessions/<sid>/events")
def session_events(handler, sid):
    s = _session_or_404(sid)
    after, limit = _page(handler)
    items = _decode_payload(_store().list_events(s["id"], after_id=after, limit=limit))
    return 200, _paged(items, after)


@route("GET", "/api/sessions/<sid>/messages")
def session_messages(handler, sid):
    s = _session_or_404(sid)
    after, limit = _page(handler)
    return 200, _paged(_store().list_session_messages(s["id"], after_id=after, limit=limit), after)


@route("POST", "/api/sessions/<sid>/messages")
def post_message(handler, sid):
    s = _session_or_404(sid)
    if s["status"] not in limits.ACTIVE_SESSION_STATUSES:
        raise ApiError(409, "invalid_state", f"session is {s['status']}")
    body = _str(_body(handler), "body", max_len=4000) or ""
    mid = _store().post_message(kind="info", body=body, from_session=None, to_session=s["id"])
    return 201, {"ok": True, "id": mid}


@route("GET", "/api/sessions/<sid>/diff")
def session_diff(handler, sid):
    s = _session_or_404(sid)
    if not s["branch"] or not s["worktree_path"]:
        raise ApiError(409, "invalid_state", "session has no branch")
    base, branch = s["base_ref"], s["branch"]
    if base.startswith("-") or not branch.startswith(worktrees.BRANCH_PREFIX):
        raise ApiError(409, "invalid_state", "invalid refs")
    repo = s["repo_path"]
    rng = f"{base}...{branch}"
    stat = worktrees._git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                          "--stat", rng, "--").stdout
    raw = worktrees._git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                         rng, "--").stdout.encode()
    uncommitted: list[str] = []
    if os.path.isdir(s["worktree_path"]) and s["status"] not in ("closed", "failed"):
        try:
            uncommitted = worktrees.inspect(repo, s["worktree_path"], base).changed
        except worktrees.WorktreeError:
            uncommitted = []
    return 200, {"ok": True, "base_ref": base, "branch": branch, "stat": stat,
                 "diff": raw[:MAX_DIFF_BYTES].decode("utf-8", "replace"),
                 "truncated": len(raw) > MAX_DIFF_BYTES, "uncommitted": uncommitted}


# ── page (multi-agent PR 8) ─────────────────────────────────────────────────
# /sessions serves web/sessions.html with a strict CSP: no inline script or
# style, same-origin fetches only. All server data is rendered with
# textContent by sessions.js, the CSP is a second line of defence.

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


def _serve_web(name: str, ctype: str, extra: dict[str, str] | None = None) -> Callable:
    def serve(handler) -> None:
        try:
            with open(os.path.join(_WEB_DIR, name), "rb") as f:
                body = f.read()
        except FileNotFoundError:
            handler._send(404, b"not found", "text/plain")
            return
        handler._send(200, body, ctype, headers=extra)
    return serve


router.add("GET", "/sessions", _serve_web("sessions.html", "text/html; charset=utf-8",
                                          {"Content-Security-Policy": PAGE_CSP}))
router.add("GET", "/sessions.css", _serve_web("sessions.css", "text/css; charset=utf-8"))
