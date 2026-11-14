"""Lessons routes for the "Aprendizaje" page (PR L4). Local-only like the rest
of /api/learning. Proposals are only created by these explicit calls (D4)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .learning import LearningError
from .learning import lessons as L
from .routes_learning import _body, _err, _local_only, _qs, _send_json
from .routing import router


def _int(v: Any) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise LearningError("bad_id", "id must be an integer")
    return v


def _post(path: str, fn: Callable[[dict], dict]) -> None:
    @_local_only
    def handler_fn(handler) -> None:
        data = _body(handler)
        if data is None:
            return
        try:
            out = fn(data)
        except LearningError as e:
            _err(handler, e.status, e.code, e.message)
            return
        _send_json(handler, 200, {"ok": True, **out})
    handler_fn.__name__ = "lessons_" + path.rsplit("/", 1)[-1].replace("-", "_")
    router.add("POST", path, handler_fn)


@router.get("/api/learning/lessons")
@_local_only
def lessons_list(handler) -> None:
    status = (_qs(handler).get("status") or [None])[0]
    try:
        rows = L.list_lessons(status=status or None)
    except LearningError as e:
        _err(handler, e.status, e.code, e.message)
        return
    _send_json(handler, 200, {"ok": True, "lessons": rows, "summary": L.summary(), "settings": L.settings(),
                              "review": L.review_suggestions(), "mirror_path": str(L.mirror_path())})


_post("/api/learning/lessons/create", lambda d: {"lesson": L.create_manual(d.get("text"), d.get("scope"))})
_post("/api/learning/lessons/approve", lambda d: {"lesson": L.approve(_int(d.get("id")), text=d.get("text"),
                                                                      scope=d.get("scope"))})
_post("/api/learning/lessons/reject", lambda d: {"lesson": L.reject(_int(d.get("id")), d.get("note"))})
_post("/api/learning/lessons/archive", lambda d: {"lesson": L.archive(_int(d.get("id")))})
_post("/api/learning/lessons/edit", lambda d: {"lesson": L.edit(_int(d.get("id")), text=d.get("text"),
                                                                scope=d.get("scope"))})


def _delete(d: dict) -> dict:
    L.delete(_int(d.get("id")))
    return {}


def _propose(d: dict) -> dict:
    fid, mode, scope = _int(d.get("feedback_id")), d.get("mode", "heuristic"), d.get("scope") or "global"
    if mode == "heuristic":
        return {"lesson": L.propose_from_feedback_heuristic(fid, scope)}
    if mode in ("llm", "ollama"):
        return {"lesson": L.propose_from_feedback_llm(fid, use_ollama=mode == "ollama", scope=scope)}
    raise LearningError("bad_mode", "mode must be heuristic, llm or ollama")


def _corrections(d: dict) -> dict:
    return {"lessons": L.propose_from_conversation(_int(d.get("conversation_id")), d.get("scope") or "global")}


def _price(d: dict) -> dict:
    L.set_price(d.get("provider"), d.get("model"), d.get("eur_in_per_mtok"), d.get("eur_out_per_mtok"))
    return {"settings": L.settings()}


def _used_by(d: dict) -> dict:
    answer = d.get("answer")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 200_000:
        raise LearningError("bad_answer", "answer text is required")
    ids = L.lessons_for_answer(answer)
    return {"lessons": [{"id": r["id"], "text": r["text"], "status": r["status"]}
                        for r in L.list_lessons(limit=1000) if r["id"] in ids]}


_post("/api/learning/lessons/delete", _delete)
_post("/api/learning/lessons/propose", _propose)
_post("/api/learning/lessons/propose-corrections", _corrections)
_post("/api/learning/lessons/settings", lambda d: {"settings": L.update_settings(d)})
_post("/api/learning/lessons/price", _price)
_post("/api/learning/lessons/used-by", _used_by)
