"""Suggest saving a successful multi-step run as a skill (gap 4, step 2).

A run is eligible when it finished successfully (status ``done``) and
recorded at least :data:`MIN_TOOL_CALLS` tool calls. A single tool call is
usually a one-off lookup, not a procedure worth turning into a skill.
The suggestion only opens the existing review panel; it never writes to
the vault. Dismissals and already-saved run ids are stored in a JSON
file under the app data directory (this machine only). No model is
called.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from .. import app_paths
from .catalog import SkillError
from .from_run import _run_id, _tool_calls

MIN_TOOL_CALLS = 2
STATE_NAME = "skill-from-run-suggestions.json"

# Serializes read-modify-write of the suggestion state in this process.
_STATE_LOCK = threading.Lock()


def state_path() -> Path:
    return app_paths.data_dir() / STATE_NAME


@contextlib.contextmanager
def _interprocess_lock(path: Path):
    """Exclusive lock in a sidecar file next to ``path`` (Windows and POSIX)."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            if os.fstat(fd).st_size < 1:
                os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
            # LK_LOCK retries for up to 10 seconds, then raises OSError.
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _empty() -> dict[str, list[int]]:
    return {"dismissed": [], "saved": []}


def _load() -> dict[str, list[int]]:
    path = state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    out = _empty()
    for key in ("dismissed", "saved"):
        vals = raw.get(key) or []
        if not isinstance(vals, list):
            continue
        seen: set[int] = set()
        for v in vals:
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if n >= 1 and n not in seen:
                out[key].append(n)
                seen.add(n)
    return out


def _dump(state: dict[str, list[int]]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"dismissed": list(state.get("dismissed") or []),
         "saved": list(state.get("saved") or [])},
        indent=2,
    )
    fd, tmp = tempfile.mkstemp(prefix=".skill-suggest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def tool_call_count(run: dict[str, Any] | None) -> int:
    if not run:
        return 0
    return len(_tool_calls(run.get("tool_calls")))


def is_eligible(run: dict[str, Any] | None) -> bool:
    """True when the run is done and has at least :data:`MIN_TOOL_CALLS` calls."""
    if not run:
        return False
    return run.get("status") == "done" and tool_call_count(run) >= MIN_TOOL_CALLS


def dismiss(run_id: Any) -> dict[str, Any]:
    """Remember that this run should not be suggested again. Does not write a skill."""
    rid = _run_id(run_id)
    path = state_path()
    with _STATE_LOCK, _interprocess_lock(path):
        state = _load()
        if rid not in state["dismissed"]:
            state["dismissed"].append(rid)
            _dump(state)
    return {"ok": True, "run_id": rid, "dismissed": True}


def mark_saved(run_id: Any) -> None:
    """Record that this run has already been saved as a skill. Not a write to the vault."""
    rid = _run_id(run_id)
    path = state_path()
    with _STATE_LOCK, _interprocess_lock(path):
        state = _load()
        if rid not in state["saved"]:
            state["saved"].append(rid)
            _dump(state)


def _reason(run: dict[str, Any] | None, state: dict[str, list[int]]) -> str | None:
    if not run:
        return "run_not_found"
    rid = int(run["id"])
    if rid in state["saved"]:
        return "already_saved"
    if rid in state["dismissed"]:
        return "dismissed"
    if run.get("status") != "done":
        return "not_done"
    if tool_call_count(run) < MIN_TOOL_CALLS:
        return "too_few_steps"
    return None


def suggestion(run_id: Any) -> dict[str, Any]:
    """Whether this run should be suggested. Never writes to the vault."""
    from .. import db_tasks

    rid = _run_id(run_id)
    run = db_tasks.get_run(rid)
    if not run:
        raise SkillError("run_not_found", f"no run with id {rid}", 404)
    state = _load()
    reason = _reason(run, state)
    count = tool_call_count(run)
    return {
        "ok": True,
        "run_id": rid,
        "suggest": reason is None,
        "reason": reason or "eligible",
        "tool_calls": count,
        "min_tool_calls": MIN_TOOL_CALLS,
    }


def suggestions_for_task(task_id: Any) -> dict[str, Any]:
    """Run ids on this task that should be suggested. Never writes to the vault."""
    from .. import db_tasks

    try:
        tid = int(task_id)
    except (TypeError, ValueError) as e:
        raise SkillError("bad_task_id", "task_id must be a positive integer") from e
    if tid < 1:
        raise SkillError("bad_task_id", "task_id must be a positive integer")
    if not db_tasks.get_task(tid):
        raise SkillError("task_not_found", f"no task with id {tid}", 404)
    state = _load()
    ids: list[int] = []
    for run in db_tasks.list_runs(tid, limit=50):
        if _reason(run, state) is None:
            ids.append(int(run["id"]))
    return {
        "ok": True,
        "task_id": tid,
        "run_ids": ids,
        "min_tool_calls": MIN_TOOL_CALLS,
    }
