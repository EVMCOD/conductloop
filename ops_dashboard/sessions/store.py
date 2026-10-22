"""Repository over the multi-agent session tables (migration 8).

Every write that must be race-free runs in one ``BEGIN IMMEDIATE`` transaction
on its own connection, so concurrent sessions (threads or processes) serialise
on SQLite's write lock:

* task claim — a single ``UPDATE … WHERE status='queued' … RETURNING``;
* path claims — conflict check + insert of all patterns, all-or-nothing;
* session creation — active-slot count + insert.

Secrets: sessions keep ``key_ref`` (a keychain item name) only; free text going
into events/messages is scrubbed of key-shaped strings before it is stored.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from . import limits, paths
from .limits import SessionLimitError, SessionValidationError
from .paths import PathConflictError

_RETURNING = sqlite3.sqlite_version_info >= (3, 35, 0)


def _ph(values: Iterable[str]) -> str:
    return ",".join("'" + v + "'" for v in values)  # constants from limits only


_ACTIVE_SQL = _ph(limits.ACTIVE_SESSION_STATUSES)
_CLAIMING_SQL = _ph(limits.CLAIMING_SESSION_STATUSES)
_IN_FLIGHT_SQL = _ph(limits.IN_FLIGHT_TASK_STATUSES)


def normalize_repo_path(repo_path: str) -> str:
    if not isinstance(repo_path, str) or not repo_path.strip():
        raise SessionValidationError("repo_path", "required")
    return os.path.normpath(os.path.abspath(os.path.expanduser(repo_path.strip())))


def _json_list(value: Iterable[Any] | None, field: str) -> str:
    items = list(value or [])
    if not all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in items):
        raise SessionValidationError(field, "must be a list of strings/ints")
    return json.dumps(items)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return limits.redact(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


class SessionStore:
    def __init__(self, connect: Callable[[], sqlite3.Connection] | None = None,
                 clock: Callable[[], float] = time.time,
                 max_iterations_limit: int | None = None):
        if connect is None:
            from .. import db as _db

            connect = _db.get_connection
        self._connect = connect
        self._clock = clock
        # None → resolved per call (env var or the default of 60).
        self._max_iterations_limit = max_iterations_limit

    # ── plumbing ──────────────────────────────────────────────────────────
    def _now(self) -> int:
        return int(self._clock())

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        conn.isolation_level = None  # explicit transaction control
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row is not None else None

    # ── repos (D7) ────────────────────────────────────────────────────────
    def mark_repo(self, repo_path: str, *, project_key: str | None = None,
                  enabled: bool = True) -> str:
        """The user marks (or unmarks) a repo as usable by sessions."""
        rp, now = normalize_repo_path(repo_path), self._now()
        with self._tx() as c:
            c.execute(
                "INSERT INTO session_repos (repo_path, project_key, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(repo_path) DO UPDATE SET "
                "project_key = COALESCE(excluded.project_key, project_key), "
                "enabled = excluded.enabled, updated_at = excluded.updated_at",
                (rp, project_key, 1 if enabled else 0, now, now))
            if not enabled:  # revoking a repo also revokes every write reservation on it
                c.execute("UPDATE path_claims SET released_at = ? "
                          "WHERE repo_path = ? AND released_at IS NULL", (now, rp))
        return rp

    def is_repo_marked(self, repo_path: str) -> bool:
        with self._read() as c:
            r = c.execute("SELECT enabled FROM session_repos WHERE repo_path = ?",
                          (normalize_repo_path(repo_path),)).fetchone()
        return bool(r and r[0])

    def list_repos(self) -> list[dict]:
        with self._read() as c:
            return [dict(r) for r in c.execute("SELECT * FROM session_repos ORDER BY repo_path")]

    def active_sessions_on_repo(self, repo_path: str) -> int:
        with self._read() as c:
            return c.execute(f"SELECT COUNT(*) FROM agent_sessions WHERE repo_path = ? "
                             f"AND status IN ({_ACTIVE_SQL})",
                             (normalize_repo_path(repo_path),)).fetchone()[0]

    # ── sessions ──────────────────────────────────────────────────────────
    def create_session(self, *, name: str, role: str, provider: str, model: str,
                       repo_path: str, base_ref: str, budget_cost_cents: int,
                       budget_currency: str = limits.BUDGET_CURRENCY,
                       budget_tokens: int | None = None,
                       max_iterations: int = limits.DEFAULT_MAX_ITERATIONS,
                       key_ref: str | None = None, scope_globs: Iterable[str] = (),
                       project_key: str | None = None, agent_member_id: int | None = None,
                       max_active: int | None = None) -> int:
        for field, val in (("name", name), ("role", role), ("provider", provider),
                           ("model", model), ("base_ref", base_ref)):
            if not isinstance(val, str) or not val.strip():
                raise SessionValidationError(field, "required")
        limits.validate_budget(budget_cost_cents, budget_currency, budget_tokens)
        limits.validate_iterations(max_iterations, self._max_iterations_limit)
        key_ref = limits.validate_key_ref(key_ref, provider)
        cap = limits.resolve_max_active(max_active)
        scope = json.dumps([paths.normalize(g) for g in scope_globs])
        rp, now = normalize_repo_path(repo_path), self._now()
        with self._tx() as c:
            r = c.execute("SELECT enabled FROM session_repos WHERE repo_path = ?", (rp,)).fetchone()
            if not (r and r[0]):
                raise SessionValidationError("repo_path", "repo is not marked for multi-agent sessions")
            active = c.execute(
                f"SELECT COUNT(*) FROM agent_sessions WHERE status IN ({_ACTIVE_SQL})").fetchone()[0]
            if active >= cap:
                raise SessionLimitError(f"{active} active sessions; limit is {cap}")
            cur = c.execute(
                "INSERT INTO agent_sessions (name, role, scope_globs, provider, model, key_ref, "
                "project_key, repo_path, base_ref, budget_currency, budget_cost_cents, "
                "budget_tokens, max_iterations, agent_member_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name.strip(), role.strip(), scope, provider.strip(), model.strip(), key_ref,
                 project_key, rp, base_ref.strip(), budget_currency, budget_cost_cents,
                 budget_tokens, max_iterations, agent_member_id, now, now))
            return int(cur.lastrowid or 0)

    def set_session_worktree(self, session_id: int, worktree_path: str, branch: str) -> bool:
        """Record the session's worktree (created by ``worktrees.create``)."""
        if not worktree_path or not branch:
            raise SessionValidationError("worktree_path", "path and branch are required")
        with self._tx() as c:
            return c.execute(
                "UPDATE agent_sessions SET worktree_path = ?, branch = ?, updated_at = ? "
                "WHERE id = ?", (str(worktree_path), branch, self._now(), session_id)).rowcount == 1

    def update_session_limits(self, session_id: int, *, budget_cost_cents: int | None = None,
                              max_iterations: int | None = None) -> dict:
        """User raises (or sets) the budget / iteration cap, e.g. before resuming.
        The budget must stay above what was already spent."""
        s = self.get_session(session_id)
        if s is None:
            raise SessionValidationError("session_id", "no such session")
        sets, args = [], []
        if budget_cost_cents is not None:
            limits.validate_budget(budget_cost_cents, s["budget_currency"], s["budget_tokens"])
            if budget_cost_cents <= s["cost_cents_est"]:
                raise SessionValidationError("budget_cost_cents",
                                             "must be above what the session already spent")
            sets.append("budget_cost_cents = ?")
            args.append(budget_cost_cents)
        if max_iterations is not None:
            limits.validate_iterations(max_iterations, self._max_iterations_limit)
            sets.append("max_iterations = ?")
            args.append(max_iterations)
        if sets:
            with self._tx() as c:
                c.execute(f"UPDATE agent_sessions SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                          [*args, self._now(), session_id])
        return self.get_session(session_id) or {}

    def in_flight_task(self, session_id: int) -> dict | None:
        with self._read() as c:
            return self._row(c.execute(
                f"SELECT * FROM session_tasks WHERE owner_session_id = ? AND status IN "
                f"({_IN_FLIGHT_SQL}) ORDER BY id LIMIT 1", (session_id,)).fetchone())

    def heartbeat(self, session_id: int) -> None:
        now = self._now()
        with self._tx() as c:
            c.execute("UPDATE agent_sessions SET heartbeat_at = ? WHERE id = ?", (now, session_id))

    def has_event(self, session_id: int, kind: str, *, task_id: int | None = None) -> bool:
        sql = "SELECT 1 FROM session_events WHERE session_id = ? AND kind = ?"
        args: list[Any] = [session_id, kind]
        if task_id is not None:
            sql += " AND task_id = ?"
            args.append(task_id)
        with self._read() as c:
            return c.execute(sql + " LIMIT 1", args).fetchone() is not None

    def model_price(self, provider: str, model: str) -> dict | None:
        with self._read() as c:
            return self._row(c.execute("SELECT * FROM model_prices WHERE provider = ? AND model = ?",
                                       (provider, model)).fetchone())

    def get_session(self, session_id: int) -> dict | None:
        """The session row plus ``usage_includes_estimates``: True when any
        model turn's tokens were estimated instead of reported by the provider."""
        with self._read() as c:
            row = self._row(c.execute("SELECT * FROM agent_sessions WHERE id = ?",
                                      (session_id,)).fetchone())
            if row is not None:
                row["usage_includes_estimates"] = c.execute(
                    "SELECT 1 FROM session_events WHERE session_id = ? AND kind = 'llm.turn' "
                    "AND json_extract(payload, '$.estimated') = 1 LIMIT 1",
                    (session_id,)).fetchone() is not None
            return row

    def list_sessions(self, *, active_only: bool = False) -> list[dict]:
        """Session rows, each with ``usage_includes_estimates`` like get_session."""
        sql = ("SELECT s.*, EXISTS (SELECT 1 FROM session_events e WHERE e.session_id = s.id "
               "AND e.kind = 'llm.turn' AND json_extract(e.payload, '$.estimated') = 1) "
               "AS usage_includes_estimates FROM agent_sessions s")
        if active_only:
            sql += f" WHERE s.status IN ({_ACTIVE_SQL})"
        with self._read() as c:
            rows = [dict(r) for r in c.execute(sql + " ORDER BY s.id")]
        for r in rows:
            r["usage_includes_estimates"] = bool(r["usage_includes_estimates"])
        return rows

    def set_session_status(self, session_id: int, status: str, *,
                           last_error: str | None = None) -> bool:
        if status not in limits.SESSION_STATUSES:
            raise SessionValidationError("status", f"unknown session status {status!r}")
        now = self._now()
        terminal = status in ("closed", "failed")
        with self._tx() as c:
            cur_row = c.execute("SELECT status FROM agent_sessions WHERE id = ?",
                                (session_id,)).fetchone()
            if cur_row is not None and cur_row[0] in ("closed", "failed") and not terminal:
                # A finished session never comes back: reactivating it would
                # also skip the concurrency-slot check done at creation.
                raise SessionValidationError("status", f"session is {cur_row[0]}; create a new one")
            cur = c.execute(
                "UPDATE agent_sessions SET status = ?, updated_at = ?, "
                "last_error = COALESCE(?, last_error), "
                "closed_at = CASE WHEN ? THEN ? ELSE closed_at END WHERE id = ?",
                (status, now, limits.redact(last_error) if last_error else None,
                 1 if terminal else 0, now, session_id))
            if terminal:  # a finished session never keeps reservations…
                c.execute("UPDATE path_claims SET released_at = ? "
                          "WHERE session_id = ? AND released_at IS NULL", (now, session_id))
                # …nor work it can no longer do: claimed/running/blocked tasks go
                # back to the queue (needs_review stays: its result is on the
                # session's branch and the user can still review it).
                c.execute("UPDATE session_tasks SET status = 'queued', owner_session_id = NULL, "
                          "updated_at = ? WHERE owner_session_id = ? "
                          "AND status IN ('claimed', 'running', 'blocked')", (now, session_id))
            return cur.rowcount == 1

    # ── tasks (D8: user assigns in v1) ────────────────────────────────────
    def enqueue_task(self, *, title: str, prompt: str, assigned_session_id: int | None = None,
                     paths_hint: Iterable[str] = (), role_hint: str | None = None,
                     priority: int = 0, depends_on: Iterable[int] = (),
                     created_by: str = "user", legacy_task_id: int | None = None) -> int:
        if not isinstance(title, str) or not title.strip():
            raise SessionValidationError("title", "required")
        hints = json.dumps([paths.normalize(p) for p in paths_hint])
        deps = _json_list(depends_on, "depends_on")
        now = self._now()
        with self._tx() as c:
            for dep in json.loads(deps):
                if c.execute("SELECT 1 FROM session_tasks WHERE id = ?", (dep,)).fetchone() is None:
                    raise SessionValidationError("depends_on", f"unknown task {dep!r}")
            cur = c.execute(
                "INSERT INTO session_tasks (title, prompt, role_hint, paths_hint, "
                "assigned_session_id, priority, depends_on, legacy_task_id, created_by, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title.strip(), prompt or "", role_hint, hints, assigned_session_id, int(priority),
                 deps, legacy_task_id, created_by, now, now))
            return int(cur.lastrowid or 0)

    def assign_task(self, task_id: int, session_id: int | None) -> bool:
        """User (re)assigns a not-yet-claimed task."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE session_tasks SET assigned_session_id = ?, updated_at = ? "
                "WHERE id = ? AND owner_session_id IS NULL AND status IN ('queued', 'blocked')",
                (session_id, self._now(), task_id))
            return cur.rowcount == 1

    def list_tasks(self, *, session_id: int | None = None, status: str | None = None,
                   after_id: int = 0, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM session_tasks WHERE id > ?"
        args: list[Any] = [int(after_id)]
        if session_id is not None:
            sql += " AND (assigned_session_id = ? OR owner_session_id = ?)"
            args += [session_id, session_id]
        if status is not None:
            sql += " AND status = ?"
            args.append(status)
        with self._read() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY id LIMIT ?",
                                               [*args, max(1, min(int(limit), 500))])]

    def cancel_task(self, task_id: int, *, reason: str = "cancelled by user") -> bool:
        """User cancels a task that isn't finished; its reservations are released.
        Callers stop a session running it first."""
        now = self._now()
        with self._tx() as c:
            cur = c.execute(
                "UPDATE session_tasks SET status = 'cancelled', result_summary = ?, updated_at = ? "
                "WHERE id = ? AND status NOT IN ('done', 'cancelled')", (reason, now, task_id))
            if cur.rowcount != 1:
                return False
            c.execute("UPDATE path_claims SET released_at = ? WHERE task_id = ? "
                      "AND released_at IS NULL", (now, task_id))
            return True

    def review_task(self, task_id: int, *, accept: bool, note: str | None = None) -> bool:
        """User review of a ``needs_review`` task. Accept → ``done``; reject →
        ``failed`` (owner cleared). Both release reservations and free the
        session. Never merges anything."""
        now = self._now()
        status = "done" if accept else "failed"
        summary = ("accepted" if accept else "rejected") + (f" by user: {note}" if note else " by user")
        with self._tx() as c:
            cur = c.execute(
                f"UPDATE session_tasks SET status = ?, "
                f"owner_session_id = {'owner_session_id' if accept else 'NULL'}, "
                "result_summary = COALESCE(result_summary, '') || ? , updated_at = ? "
                "WHERE id = ? AND status = 'needs_review'",
                (status, f"\n\n[{limits.redact(summary)}]", now, task_id))
            if cur.rowcount != 1:
                return False
            c.execute("UPDATE path_claims SET released_at = ? WHERE task_id = ? "
                      "AND released_at IS NULL", (now, task_id))
            return True

    def get_task(self, task_id: int) -> dict | None:
        with self._read() as c:
            return self._row(c.execute("SELECT * FROM session_tasks WHERE id = ?",
                                       (task_id,)).fetchone())

    _CLAIM_WHERE = (
        "status = 'queued' AND owner_session_id IS NULL AND assigned_session_id = :sid "
        f"AND EXISTS (SELECT 1 FROM agent_sessions s WHERE s.id = :sid AND s.status IN ({_CLAIMING_SQL})) "
        "AND NOT EXISTS (SELECT 1 FROM session_tasks o WHERE o.owner_session_id = :sid "
        f"  AND o.status IN ({_IN_FLIGHT_SQL})) "
        # A dependency that does not exist (any more) blocks too.
        "AND NOT EXISTS (SELECT 1 FROM json_each(session_tasks.depends_on) d "
        "  LEFT JOIN session_tasks x ON x.id = d.value WHERE x.id IS NULL OR x.status <> 'done')"
    )

    def claim_task(self, session_id: int, task_id: int | None = None) -> dict | None:
        """Atomically give one queued task to ``session_id``: the given task, or
        its highest-priority assigned one. Returns the task or None. A session
        holds at most one in-flight task; dependencies must be done."""
        params: dict[str, Any] = {"sid": session_id, "now": self._now(), "tid": task_id}
        pick = "id = :tid" if task_id is not None else (
            "id = (SELECT id FROM session_tasks WHERE " + self._CLAIM_WHERE +
            " ORDER BY priority DESC, id LIMIT 1)")
        sql = ("UPDATE session_tasks SET owner_session_id = :sid, status = 'claimed', "
               "attempts = attempts + 1, claimed_at = :now, updated_at = :now "
               f"WHERE {pick} AND {self._CLAIM_WHERE}")
        with self._tx() as c:
            if _RETURNING:
                row = c.execute(sql + " RETURNING *", params).fetchone()
                return self._row(row)
            cur = c.execute(sql, params)  # pragma: no cover - SQLite < 3.35
            if cur.rowcount != 1:
                return None
            return self._row(c.execute(
                "SELECT * FROM session_tasks WHERE owner_session_id = :sid AND claimed_at = :now "
                "ORDER BY updated_at DESC, id DESC LIMIT 1", params).fetchone())

    def set_task_status(self, task_id: int, session_id: int, status: str, *,
                        result_summary: str | None = None) -> bool:
        """Owner-only transition. ``queued``/``failed`` give the task back (no
        owner); ``done``/``cancelled``/``queued``/``failed`` release its claims."""
        if status not in limits.TASK_STATUSES or status == "claimed":
            raise SessionValidationError("status", f"invalid task status {status!r}")
        now = self._now()
        release = status in ("done", "cancelled", "queued", "failed")
        owner_expr = "NULL" if status in ("queued", "failed") else "owner_session_id"
        with self._tx() as c:
            cur = c.execute(
                f"UPDATE session_tasks SET status = ?, owner_session_id = {owner_expr}, "
                "result_summary = COALESCE(?, result_summary), updated_at = ? "
                # A user cancel (or accept) is final: a runner that was still
                # finishing a provider call must not overwrite it.
                "WHERE id = ? AND owner_session_id = ? AND status NOT IN ('cancelled', 'done')",
                (status, limits.redact(result_summary) if result_summary else None,
                 now, task_id, session_id))
            if cur.rowcount != 1:
                return False
            if release:
                c.execute("UPDATE path_claims SET released_at = ? "
                          "WHERE task_id = ? AND released_at IS NULL", (now, task_id))
            return True

    # ── path claims (D3) ──────────────────────────────────────────────────
    @staticmethod
    def _active_claims_sql() -> str:
        return ("SELECT * FROM path_claims WHERE repo_path = ? AND released_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?)")

    def claim_paths(self, session_id: int, patterns: Iterable[str], *,
                    task_id: int | None = None, ttl_seconds: int | None = None,
                    overlap: Callable[[str, str], bool] | None = None) -> list[dict]:
        """Reserve ``patterns`` for writing, all-or-nothing. ``overlap`` replaces
        the conservative pattern-only check (e.g. ``paths.precise_overlap`` bound
        to the worktree's file list). Hot files and
        lockfiles are always ``exclusive`` (one task at a time, even within a
        session); other claims are ``write`` (shareable only by the same
        session). Raises PathConflictError and grants nothing on any clash."""
        wanted = list(dict.fromkeys(paths.normalize(p) for p in patterns))
        if not wanted:
            raise SessionValidationError("patterns", "at least one path is required")
        now = self._now()
        expires = now + int(ttl_seconds) if ttl_seconds else None
        with self._tx() as c:
            s = c.execute("SELECT repo_path, status FROM agent_sessions WHERE id = ?",
                          (session_id,)).fetchone()
            if s is None or s["status"] not in limits.ACTIVE_SESSION_STATUSES:
                raise SessionValidationError("session_id", "no such active session")
            repo = s["repo_path"]
            enabled = c.execute("SELECT enabled FROM session_repos WHERE repo_path = ?",
                                (repo,)).fetchone()
            if not (enabled and enabled[0]):
                raise SessionValidationError("repo_path", "this repo is no longer enabled for sessions")
            if task_id is not None and c.execute(
                    "SELECT 1 FROM session_tasks WHERE id = ? AND (owner_session_id = ? OR "
                    "(owner_session_id IS NULL AND assigned_session_id = ?))",
                    (task_id, session_id, session_id)).fetchone() is None:
                # Otherwise a session could tag a claim with another session's
                # task, and that task finishing would release it.
                raise SessionValidationError("task_id", "the task does not belong to this session")
            held = [dict(r) for r in c.execute(self._active_claims_sql(), (repo, now))]
            conflicts, granted_existing, to_insert = [], [], []
            for p in wanted:
                mode = "exclusive" if paths.is_exclusive(p) else "write"
                same = next((h for h in held if h["session_id"] == session_id
                             and h["task_id"] == task_id and h["pattern"] == p), None)
                if same is not None:
                    granted_existing.append(same)
                    continue
                for h in held:
                    if not (overlap or paths.overlaps)(p, h["pattern"]):
                        continue
                    other_session = h["session_id"] != session_id
                    exclusive_clash = ("exclusive" in (mode, h["mode"])) and h["task_id"] != task_id
                    if other_session or exclusive_clash:
                        conflicts.append({"pattern": p, "mode": mode,
                                          "held_pattern": h["pattern"], "held_mode": h["mode"],
                                          "held_session_id": h["session_id"],
                                          "held_task_id": h["task_id"], "held_claim_id": h["id"]})
                to_insert.append((p, mode))
            if conflicts:
                raise PathConflictError(conflicts)
            out = list(granted_existing)
            for p, mode in to_insert:
                cur = c.execute(
                    "INSERT INTO path_claims (session_id, task_id, repo_path, pattern, mode, "
                    "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (session_id, task_id, repo, p, mode, now, expires))
                out.append(dict(c.execute("SELECT * FROM path_claims WHERE id = ?",
                                          (cur.lastrowid,)).fetchone()))
            return out

    def release_paths(self, session_id: int, *, task_id: int | None = None,
                      claim_ids: Iterable[int] | None = None) -> int:
        sql, args = ("UPDATE path_claims SET released_at = ? WHERE session_id = ? "
                     "AND released_at IS NULL"), [self._now(), session_id]
        if task_id is not None:
            sql += " AND task_id = ?"
            args.append(task_id)
        ids = [int(i) for i in claim_ids] if claim_ids is not None else None
        if ids is not None:
            if not ids:
                return 0
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            args.extend(ids)
        with self._tx() as c:
            return c.execute(sql, args).rowcount

    def active_claims(self, session_id: int | None = None,
                      repo_path: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM path_claims WHERE released_at IS NULL "
               "AND (expires_at IS NULL OR expires_at > ?)")
        args: list[Any] = [self._now()]
        if session_id is not None:
            sql += " AND session_id = ?"
            args.append(session_id)
        if repo_path is not None:
            sql += " AND repo_path = ?"
            args.append(normalize_repo_path(repo_path))
        with self._read() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY id", args)]

    def can_write(self, session_id: int, path: str, *, task_id: int | None = None) -> bool:
        """Enforcement primitive for fs_write/apply_patch (D3): writing needs an
        active claim of this session covering ``path``, on a repo that is still
        enabled. A hot file / lockfile needs an exclusive claim of its own: a
        broad directory claim is ``write`` mode, which two tasks may both hold."""
        hot = paths.is_exclusive(path)
        enabled: dict[str, bool] = {}
        for cl in self.active_claims(session_id=session_id):
            if task_id is not None and cl["task_id"] not in (None, task_id):
                continue
            if hot and cl["mode"] != "exclusive":
                continue
            if not paths.covers(cl["pattern"], path):
                continue
            rp = cl["repo_path"]
            if rp not in enabled:
                enabled[rp] = self.is_repo_marked(rp)
            if enabled[rp]:
                return True
        return False

    # ── messages & events ─────────────────────────────────────────────────
    def post_message(self, *, kind: str, body: str, from_session: int | None = None,
                     to_session: int | None = None, task_id: int | None = None) -> int:
        if kind not in limits.MESSAGE_KINDS:
            raise SessionValidationError("kind", f"unknown message kind {kind!r}")
        if not isinstance(body, str) or not body.strip():
            raise SessionValidationError("body", "required")
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO session_messages (from_session, to_session, task_id, kind, body, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (from_session, to_session, task_id, kind, limits.redact(body), self._now()))
            return int(cur.lastrowid or 0)

    def list_messages(self, to_session: int | None, *, unread_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM session_messages WHERE to_session IS ?"
        if unread_only:
            sql += " AND read_at IS NULL"
        with self._read() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY id", (to_session,))]

    def list_session_messages(self, session_id: int | None, *, after_id: int = 0,
                              limit: int = 100) -> list[dict]:
        """Messages to/from a session (``None`` → the user's inbox: to nobody)."""
        if session_id is None:
            sql = "SELECT * FROM session_messages WHERE to_session IS NULL AND id > ?"
            args: list[Any] = [after_id]
        else:
            sql = ("SELECT * FROM session_messages WHERE (to_session = ? OR from_session = ?) "
                   "AND id > ?")
            args = [session_id, session_id, after_id]
        with self._read() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY id LIMIT ?",
                                               [*args, max(1, min(int(limit), 500))])]

    def mark_read(self, message_ids: Iterable[int]) -> int:
        ids = [int(i) for i in message_ids]
        if not ids:
            return 0
        with self._tx() as c:
            return c.execute(
                f"UPDATE session_messages SET read_at = ? WHERE read_at IS NULL "
                f"AND id IN ({','.join('?' * len(ids))})", [self._now(), *ids]).rowcount

    def record_event(self, session_id: int, kind: str, payload: Any = None, *,
                     task_id: int | None = None, tokens_in: int = 0, tokens_out: int = 0,
                     iteration: bool = False) -> dict:
        """Append to the timeline and add usage to the session totals in the same
        transaction. Cost uses the user's model_prices; unknown price → 0 and
        ``price_missing``. Returns {id, cost_cents_est, price_missing,
        budget_ratio, iterations_left}."""
        if not isinstance(kind, str) or not kind.strip():
            raise SessionValidationError("kind", "required")
        tin, tout = max(0, int(tokens_in)), max(0, int(tokens_out))
        body = json.dumps(_scrub(payload), default=str) if payload is not None else None
        now = self._now()
        with self._tx() as c:
            s = c.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
            if s is None:
                raise SessionValidationError("session_id", "no such session")
            price = c.execute("SELECT in_cents_per_mtok, out_cents_per_mtok FROM model_prices "
                              "WHERE provider = ? AND model = ?", (s["provider"], s["model"])).fetchone()
            missing = price is None and (tin or tout) > 0 and s["provider"] != "ollama"
            cost = 0.0 if price is None else (tin * price[0] + tout * price[1]) / 1_000_000
            cur = c.execute(
                "INSERT INTO session_events (session_id, task_id, kind, payload, tokens_in, "
                "tokens_out, cost_cents_est, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, task_id, kind.strip(), body, tin, tout, cost, now))
            c.execute("UPDATE agent_sessions SET tokens_in = tokens_in + ?, "
                      "tokens_out = tokens_out + ?, cost_cents_est = cost_cents_est + ?, "
                      "iterations = iterations + ?, heartbeat_at = ?, updated_at = ? WHERE id = ?",
                      (tin, tout, cost, 1 if iteration else 0, now, now, session_id))
            t = c.execute("SELECT cost_cents_est, budget_cost_cents, iterations, max_iterations "
                          "FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
        return {"id": int(cur.lastrowid or 0), "cost_cents_est": cost, "price_missing": missing,
                "budget_ratio": t["cost_cents_est"] / t["budget_cost_cents"],
                "iterations_left": t["max_iterations"] - t["iterations"]}

    def list_events(self, session_id: int, *, after_id: int = 0, limit: int = 200) -> list[dict]:
        with self._read() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM session_events WHERE session_id = ? AND id > ? ORDER BY id LIMIT ?",
                (session_id, after_id, max(1, min(int(limit), 1000))))]

    # ── prices (user-entered only) ────────────────────────────────────────
    def set_model_price(self, provider: str, model: str, *, in_cents_per_mtok: float,
                        out_cents_per_mtok: float, currency: str = limits.BUDGET_CURRENCY) -> None:
        if currency != limits.BUDGET_CURRENCY:
            raise SessionValidationError("currency", "prices are in EUR")
        for f, v in (("in_cents_per_mtok", in_cents_per_mtok),
                     ("out_cents_per_mtok", out_cents_per_mtok)):
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                raise SessionValidationError(f, "must be a non-negative number")
        with self._tx() as c:
            c.execute(
                "INSERT INTO model_prices (provider, model, currency, in_cents_per_mtok, "
                "out_cents_per_mtok, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider, model) DO UPDATE SET currency = excluded.currency, "
                "in_cents_per_mtok = excluded.in_cents_per_mtok, "
                "out_cents_per_mtok = excluded.out_cents_per_mtok, updated_at = excluded.updated_at",
                (provider, model, currency, float(in_cents_per_mtok), float(out_cents_per_mtok),
                 self._now()))

    def list_model_prices(self) -> list[dict]:
        with self._read() as c:
            return [dict(r) for r in c.execute("SELECT * FROM model_prices ORDER BY provider, model")]
