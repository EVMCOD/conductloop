"""Learning routes (PR L3): 👍/👎 feedback, the opt-in failure log and the
"Aprendizaje" page. Registered on the shared route registry (imported from
routes_meta), so server.py does not grow.

All routes are **local-only** (loopback, no X-Forwarded-For/X-Relay): feedback
and failure codes describe how the owner uses the app and never need to be
reachable through a tunnel or from a paired device. Remote requests get 403
``{"ok": false, "error": "local_only"}``.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable

from . import learning
from .routing import router

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")
MAX_BODY = 256 * 1024


def _send_json(handler, status: int, obj: dict) -> None:
    handler._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                  "application/json; charset=utf-8")


def _err(handler, status: int, code: str, message: str) -> None:
    _send_json(handler, status, {"ok": False, "error": code, "message": message})


def _local_only(fn: Callable) -> Callable:
    def wrapper(handler) -> None:
        if not handler._is_local():
            _err(handler, 403, "local_only", "learning data is only available on this Mac")
            return
        fn(handler)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _body(handler) -> dict | None:
    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
    except ValueError:
        length = -1
    if length < 0 or length > MAX_BODY:
        _err(handler, 413, "body_too_large", f"body must be <= {MAX_BODY} bytes")
        return None
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        _err(handler, 400, "bad_json", "body must be a JSON object")
        return None
    if not isinstance(data, dict):
        _err(handler, 400, "bad_json", "body must be a JSON object")
        return None
    return data


def _qs(handler) -> dict:
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(handler.path).query)


def _int_arg(qs: dict, name: str, default: int) -> int:
    try:
        return int((qs.get(name) or [str(default)])[0])
    except ValueError:
        return default


# ---------- page ----------

def _serve_web(name: str, ctype: str, headers: dict | None = None) -> Callable:
    @_local_only
    def serve(handler) -> None:
        with open(os.path.join(_WEB_DIR, name), "rb") as fh:
            body = fh.read()
        handler._send(200, body, ctype, headers or None)
    serve.__name__ = f"serve_{name.replace('.', '_').replace('-', '_')}"
    return serve


router.add("GET", "/learning", _serve_web("learning.html", "text/html; charset=utf-8",
                                          {"Content-Security-Policy": PAGE_CSP}))
router.add("GET", "/learning.css", _serve_web("learning.css", "text/css; charset=utf-8"))


# ---------- API ----------

@router.get("/api/learning/summary")
@_local_only
def learning_summary(handler) -> None:
    _send_json(handler, 200, {"ok": True, **learning.summary()})


@router.get("/api/learning/settings")
@_local_only
def learning_settings_get(handler) -> None:
    _send_json(handler, 200, {"ok": True, "failure_log_enabled": learning.failure_log_enabled(),
                              "retention_days": learning.RETENTION_DAYS})


@router.post("/api/learning/settings")
@_local_only
def learning_settings_set(handler) -> None:
    data = _body(handler)
    if data is None:
        return
    v = data.get("failure_log_enabled")
    if not isinstance(v, bool):
        _err(handler, 400, "bad_setting", "failure_log_enabled must be true or false")
        return
    learning.set_failure_log_enabled(v)
    _send_json(handler, 200, {"ok": True, "failure_log_enabled": learning.failure_log_enabled(),
                              "retention_days": learning.RETENTION_DAYS})


@router.post("/api/learning/feedback")
@_local_only
def learning_feedback_post(handler) -> None:
    """Body: {target_kind:'brain_message'|'team_message', message_id | (answer [, conversation_id]),
    rating: 1|-1, reason_code?, comment?}"""
    data = _body(handler)
    if data is None:
        return
    try:
        kind = data.get("target_kind", "brain_message")
        if kind not in learning.store.SUPPORTED_TARGETS:
            raise learning.LearningError("unsupported_target",
                                         f"target_kind must be one of {learning.store.SUPPORTED_TARGETS}")
        rating = data.get("rating")
        if not isinstance(rating, int) or isinstance(rating, bool):
            raise learning.LearningError("bad_rating", "rating must be 1 or -1")
        target_id = learning.resolve_brain_message(message_id=data.get("message_id"),
                                                   conversation_id=data.get("conversation_id"),
                                                   answer=data.get("answer"))
        row = learning.record_feedback(target_kind=kind, target_id=target_id, rating=rating,
                                       reason_code=data.get("reason_code") or None,
                                       comment=data.get("comment"))
    except learning.LearningError as e:
        _err(handler, e.status, e.code, e.message)
        return
    _send_json(handler, 200, {"ok": True, "feedback": row})


@router.post("/api/brain/citation-open")
@_local_only
def brain_citation_open(handler) -> None:
    """Record that the user opened one of the citations of an answer.

    Body: {answer, path, source?, conversation_id?, message_id?, event_kind?}

    This is the only signal that says which retrieved document was the useful
    one, so it has to survive the navigation that follows the click. It never
    errors on the UI: an answer that cannot be resolved is still recorded with
    its path, and the response says so.
    """
    from . import db as _db
    from .learning import ranker

    data = _body(handler)
    if data is None:
        data = {}
    path = data.get("path")
    if not isinstance(path, str) or not path.strip():
        _err(handler, 400, "bad_path", "path is required")
        return

    message_id = data.get("message_id")
    conversation_id = data.get("conversation_id")
    try:
        resolved = learning.resolve_brain_message(message_id=message_id,
                                                   conversation_id=conversation_id,
                                                   answer=data.get("answer"))
    except learning.LearningError:
        resolved = None

    # The click belongs to the message that carries the citation list, and that
    # is not always the message the user was reading: in the team chat the
    # citations live on the plan while the click arrives on the agent's reply.
    anchor = ranker.anchor_message(resolved, conversation_id)

    try:
        row = ranker.record_event(path=path, source=data.get("source"),
                                  message_id=anchor if anchor is not None else resolved,
                                  conversation_id=conversation_id,
                                  event_kind=data.get("event_kind") or "open")
    except ranker.RankerError as e:
        _err(handler, e.status, e.code, e.message)
        return

    doc_id = None
    try:
        with _db.db() as conn:
            for key in ranker._lookup_keys(path):
                found = conn.execute("SELECT id FROM documents WHERE path = ? LIMIT 1", (key,)).fetchone()
                if found:
                    doc_id = int(found["id"])
                    break
    except Exception:
        doc_id = None

    _send_json(handler, 200, {"ok": True, "doc_id": doc_id, "event_id": row["id"],
                              "resolved": resolved is not None, "message_id": anchor})


@router.get("/api/learning/ranker")
@_local_only
def learning_ranker_status(handler) -> None:
    """What the citation re-ranker knows right now."""
    from .learning import ranker
    _send_json(handler, 200, {"ok": True, **ranker.model_status()})


@router.post("/api/learning/ranker/train")
@_local_only
def learning_ranker_train(handler) -> None:
    """Retrain the citation re-ranker from the collected clicks."""
    from .learning import ranker
    try:
        report = ranker.train()
    except ranker.RankerError as e:
        _err(handler, e.status, e.code, e.message)
        return
    _send_json(handler, 200, {"ok": True, "report": report, **ranker.model_status()})


@router.get("/api/learning/feedback")
@_local_only
def learning_feedback_list(handler) -> None:
    qs = _qs(handler)
    rating = _int_arg(qs, "rating", 0)
    _send_json(handler, 200, {"ok": True, "feedback": learning.list_feedback(
        limit=_int_arg(qs, "limit", 50), rating=rating if rating in (-1, 1) else None)})


@router.post("/api/learning/feedback/delete")
@_local_only
def learning_feedback_delete(handler) -> None:
    data = _body(handler)
    if data is None:
        return
    fid = data.get("id")
    if not isinstance(fid, int) or isinstance(fid, bool):
        _err(handler, 400, "bad_id", "id must be an integer")
        return
    if not learning.delete_feedback(fid):
        _err(handler, 404, "not_found", "feedback not found")
        return
    _send_json(handler, 200, {"ok": True})


@router.get("/api/learning/failures")
@_local_only
def learning_failures_list(handler) -> None:
    _send_json(handler, 200, {"ok": True, "enabled": learning.failure_log_enabled(),
                              "retention_days": learning.RETENTION_DAYS,
                              "failures": learning.list_failures(limit=_int_arg(_qs(handler), "limit", 100))})


@router.post("/api/learning/failures/clear")
@_local_only
def learning_failures_clear(handler) -> None:
    _send_json(handler, 200, {"ok": True, "deleted": learning.clear_failures()})


@router.post("/api/learning/export")
@_local_only
def learning_export(handler) -> None:
    data = _body(handler)
    if data is None:
        return
    path = learning.export_local(include_comments=data.get("include_comments") is True)
    _send_json(handler, 200, {"ok": True, "path": str(path)})


@router.get("/api/learning/evals")
@_local_only
def learning_evals(handler) -> None:
    from .learning import evals_view  # L7: read-only, never runs the harness
    _send_json(handler, 200, {"ok": True, **evals_view.overview()})


@router.get("/api/learning/metrics")
@_local_only
def learning_metrics(handler) -> None:
    from .learning import metrics  # L8
    raw = (_qs(handler).get("days") or ["30"])[0]
    try:
        out = metrics.report(int(raw) if raw.isdigit() else -1)
    except learning.LearningError as e:
        _err(handler, e.status, e.code, e.message)
        return
    _send_json(handler, 200, {"ok": True, **out})


@router.post("/api/learning/metrics/settings")
@_local_only
def learning_metrics_settings(handler) -> None:
    from .learning import metrics  # L8
    data = _body(handler)
    if data is None:
        return
    try:
        out = metrics.update_settings(data)
    except learning.LearningError as e:
        _err(handler, e.status, e.code, e.message)
        return
    _send_json(handler, 200, {"ok": True, "settings": out})


from . import (
    routes_lessons,  # noqa: E402,F401  (L4: registers /api/learning/lessons*)
    routes_skill_proposals,  # noqa: E402,F401  (L6: registers /api/learning/skills/proposals*)
    routes_skills,  # noqa: E402,F401  (L5: registers /api/learning/skills*)
    routes_skills_from_run,  # noqa: E402,F401  (gap 4 step 1: /api/learning/skills/from-run*)
    routes_skills_url,  # noqa: E402,F401  (gap 3 step 2: /api/learning/skills/import-url*)
)
