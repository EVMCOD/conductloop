"""Choose the Holding CEO (Boardroom, 2026-11-06).

GET  /api/boardroom/holding-ceo   {"holding_ceo": {id,name,role}|null, "candidates": [...]}
POST /api/boardroom/holding-ceo   {"member_id": n|null}: choose an existing AI agent, or
                                  clear it (null) so the Boardroom's own reply answers

The chairman only: this machine, or a signed-in chairman/admin elsewhere.
"""
from __future__ import annotations

import json
from typing import Any

from . import boardroom
from .routing import router

FALLBACK = "the Boardroom's Holding CEO reply"
MAX_BODY = 4096


def _send(handler: Any, status: int, obj: dict) -> None:
    handler._send(status, json.dumps(obj, ensure_ascii=False).encode(),
                  "application/json; charset=utf-8")


def _err(handler: Any, status: int, code: str, message: str) -> None:
    _send(handler, status, {"ok": False, "error": code, "message": message})


def _user(handler: Any) -> dict | None:
    try:
        return handler._current_user()
    except Exception:
        return None


def _allowed(handler: Any) -> bool:
    if handler._is_local():
        return True
    ok, _ = boardroom.require_chairman(_user(handler))
    return ok


def _state() -> dict:
    return {"ok": True, "holding_ceo": boardroom.holding_ceo(),
            "candidates": boardroom.holding_ceo_candidates(), "fallback": FALLBACK}


@router.get("/api/boardroom/holding-ceo")
def get_holding_ceo(handler: Any) -> None:
    if not _allowed(handler):
        _err(handler, 403, "chairman_only", "only the chairman can see this")
        return
    _send(handler, 200, _state())


@router.post("/api/boardroom/holding-ceo")
def set_holding_ceo(handler: Any) -> None:
    if not _allowed(handler):
        _err(handler, 403, "chairman_only", "only the chairman can choose the Holding CEO")
        return
    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
    except (TypeError, ValueError, AttributeError):
        length = -1
    if length < 0 or length > MAX_BODY:
        _err(handler, 400, "bad_request", "body must be a small JSON object")
        return
    try:
        body = json.loads((handler.rfile.read(length) if length else b"{}").decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        body = None
    if not isinstance(body, dict) or "member_id" not in body:
        _err(handler, 400, "bad_request", 'send {"member_id": <AI agent id> or null}')
        return
    raw = body["member_id"]
    member_id: int | None = None
    if raw not in (None, ""):
        if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
            raw = "x"
        try:
            member_id = int(raw)
        except (TypeError, ValueError):
            _err(handler, 400, "not_an_ai_agent", "member_id: the id of an existing AI agent")
            return
    try:
        boardroom.set_holding_ceo(member_id, user=_user(handler))
    except ValueError:
        _err(handler, 400, "not_an_ai_agent", "member_id: the id of an existing AI agent")
        return
    _send(handler, 200, _state())
