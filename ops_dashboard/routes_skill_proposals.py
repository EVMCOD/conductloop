"""Skill proposal queue routes for the "Aprendizaje" page (PR L6). Local-only
like the rest of /api/learning. Every POST that creates a proposal is an
explicit user action (D4); only /approve writes to disk (D3)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .learning import LearningError
from .learning import lessons as L
from .routes_learning import _body, _err, _local_only, _qs, _send_json
from .routing import router
from .skills import propose as P
from .skills.catalog import SkillError

BASE = "/api/learning/skills/proposals"


def _int(v: Any) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise SkillError("bad_id", "id must be an integer")
    return v


def _post(path: str, fn: Callable[[dict], dict]) -> None:
    @_local_only
    def handler_fn(handler) -> None:
        data = _body(handler)
        if data is None:
            return
        try:
            out = fn(data)
        except (SkillError, LearningError) as e:
            _err(handler, e.status, e.code, e.message)
            return
        _send_json(handler, 200, {"ok": True, **out})
    handler_fn.__name__ = "skill_proposals_" + path.rsplit("/", 1)[-1].replace("-", "_")
    router.add("POST", path, handler_fn)


@router.get(BASE)
@_local_only
def skill_proposals_list(handler) -> None:
    status = (_qs(handler).get("status") or [None])[0]
    try:
        rows = P.list_proposals(status or None)
    except SkillError as e:
        _err(handler, e.status, e.code, e.message)
        return
    _send_json(handler, 200, {"ok": True, "proposals": rows, "summary": P.summary(),
                              "agent_skills": P.agent_skills(), "unused_days": P.UNUSED_DAYS,
                              "budget_eur": L.settings()["proposal_budget_eur"]})


def _p(d: dict) -> dict:
    return {"proposal": d}


_post(f"{BASE}/edit", lambda d: _p(P.edit(_int(d.get("id")), d.get("content"), d.get("files"))))
_post(f"{BASE}/approve", lambda d: _p(P.approve(_int(d.get("id")), d.get("content"), d.get("files"))))
_post(f"{BASE}/reject", lambda d: _p(P.reject(_int(d.get("id")), d.get("note"))))
_post(f"{BASE}/from-conversation",
      lambda d: _p(P.from_conversation(_int(d.get("conversation_id")), d.get("mode", "llm"))))
_post(f"{BASE}/from-source", lambda d: _p(P.from_source(d.get("kind", ""), d.get("ref"), d.get("mode", "heuristic"))))
_post(f"{BASE}/patch",
      lambda d: _p(P.patch_from_feedback(d.get("skill_name"), _int(d.get("feedback_id")), d.get("mode", "llm"))))
_post(f"{BASE}/archive", lambda d: _p(P.request_archive(d.get("skill_name"))))
_post(f"{BASE}/delete", lambda d: _p(P.request_delete(d.get("skill_name"))))
_post(f"{BASE}/curator", lambda d: {"proposals": P.curator_scan()})
