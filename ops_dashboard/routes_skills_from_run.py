"""Local-only routes for saving a finished run as a skill, and for
suggesting that save after a successful multi-step run (gap 4)."""
from __future__ import annotations

from .routes_learning import _body, _err, _local_only, _qs, _send_json
from .routing import router
from .skills import catalog
from .skills.from_run import preview, save
from .skills.suggest_from_run import dismiss, suggestion, suggestions_for_task


def _reindex() -> dict:
    from .indexers import skills as skills_indexer
    return skills_indexer.run()


def _fail(handler, e: catalog.SkillError) -> None:
    _err(handler, e.status, e.code, e.message)


@router.post("/api/learning/skills/from-run")
@_local_only
def skills_from_run_preview(handler) -> None:
    """Body: {run_id}. Return a draft skill. Does not write to the vault."""
    data = _body(handler)
    if data is None:
        return
    try:
        result = preview(data.get("run_id"))
    except catalog.SkillError as e:
        _fail(handler, e)
        return
    _send_json(handler, 200, result)


@router.post("/api/learning/skills/from-run/save")
@_local_only
def skills_from_run_save(handler) -> None:
    """Body: {run_id, name, description, body}. Write only after this call."""
    data = _body(handler)
    if data is None:
        return
    try:
        result = save(
            data.get("run_id"),
            data.get("name"),
            data.get("description"),
            data.get("body"),
        )
    except catalog.SkillError as e:
        _fail(handler, e)
        return
    from .routes_skills import _state
    counters = _reindex()
    _send_json(handler, 200, {**_state(), **result, "counters": counters})


@router.get("/api/learning/skills/from-run/suggest")
@_local_only
def skills_from_run_suggest(handler) -> None:
    """Query: task_id or run_id. Which finished multi-step runs to suggest."""
    qs = _qs(handler)
    task_id = (qs.get("task_id") or [None])[0]
    run_id = (qs.get("run_id") or [None])[0]
    try:
        if task_id not in (None, ""):
            result = suggestions_for_task(task_id)
        elif run_id not in (None, ""):
            result = suggestion(run_id)
        else:
            _err(handler, 400, "bad_id", "task_id or run_id is required")
            return
    except catalog.SkillError as e:
        _fail(handler, e)
        return
    _send_json(handler, 200, result)


@router.post("/api/learning/skills/from-run/dismiss")
@_local_only
def skills_from_run_dismiss(handler) -> None:
    """Body: {run_id}. Do not suggest this run again. Does not write a skill."""
    data = _body(handler)
    if data is None:
        return
    try:
        result = dismiss(data.get("run_id"))
    except catalog.SkillError as e:
        _fail(handler, e)
        return
    _send_json(handler, 200, result)
