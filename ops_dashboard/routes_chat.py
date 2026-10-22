"""HTTP routes for the team chat page (/chat). Logic lives in team_chat.py.

Local only, like the Aprendizaje routes: conversations are private data.
POST /api/chat/send answers with Server-Sent Events, one ``data:`` frame per
team_chat event.
"""
from __future__ import annotations

import json

from . import brain, team_chat
from . import provider_errors as _pe
from .routes_learning import PAGE_CSP, _body, _err, _qs, _send_json, _serve_web
from .routing import router


def _local_only(fn):
    """403 unless the request comes from this Mac; passes route params through."""
    def wrapper(handler, **params) -> None:
        if not handler._is_local():
            _err(handler, 403, "local_only", "chat is only available on this Mac")
            return
        fn(handler, **params)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


@_local_only
def agents(handler) -> None:
    info = team_chat.roster()
    try:
        providers = brain.list_providers_status()
    except Exception:
        providers = []
    _send_json(handler, 200, {"ok": True, **info, "providers": providers})


@_local_only
def threads(handler) -> None:
    agent = (_qs(handler).get("agent") or [""])[0] or None
    _send_json(handler, 200, {"ok": True, "threads": team_chat.list_threads(agent)})


def _thread_id(raw: str) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@_local_only
def thread(handler, thread_id) -> None:
    tid = _thread_id(thread_id)
    data = team_chat.get_thread(tid) if tid is not None else None
    if data is None:
        _err(handler, 404, "not_found", "no such conversation")
        return
    _send_json(handler, 200, {"ok": True, "thread": data})


@_local_only
def delete_thread(handler, thread_id) -> None:
    tid = _thread_id(thread_id)
    if tid is None or not team_chat.delete_thread(tid):
        _err(handler, 404, "not_found", "no such conversation")
        return
    _send_json(handler, 200, {"ok": True})


@_local_only
def send(handler) -> None:
    data = _body(handler)
    if data is None:
        return
    thread_id = data.get("thread_id")
    if thread_id is not None and not isinstance(thread_id, int):
        _err(handler, 400, "bad_request", "thread_id must be an integer")
        return
    try:
        turn = team_chat.ChatTurn(
            str(data.get("agent") or ""), str(data.get("message") or ""),
            thread_id=thread_id, team=data.get("team", True) is not False,
            provider=(str(data["provider"]) if data.get("provider") else None),
        )
    except _pe.ProviderUnavailableError as e:
        _pe.send(handler, e)
        return
    except ValueError as e:
        _err(handler, 400, "bad_request", str(e))
        return
    except LookupError as e:
        _err(handler, 404, "not_found", str(e))
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    try:
        for event in turn.events():
            handler.wfile.write(b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n")
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        return    # the page went away; agent threads finish on their own
    except Exception as e:
        frame = {"type": "error", "code": "internal", "message": f"{type(e).__name__}: {e}"}
        try:
            handler.wfile.write(b"data: " + json.dumps(frame).encode() + b"\n\n")
            handler.wfile.flush()
        except OSError:
            pass


router.add("GET", "/chat", _serve_web("chat.html", "text/html; charset=utf-8",
                                      {"Content-Security-Policy": PAGE_CSP}))
router.add("GET", "/chat.css", _serve_web("chat.css", "text/css; charset=utf-8"))
router.add("GET", "/chat.js", _serve_web("chat.js", "text/javascript; charset=utf-8"))
router.add("GET", "/api/chat/agents", agents)
router.add("GET", "/api/chat/threads", threads)
router.add("GET", "/api/chat/threads/<thread_id>", thread)
router.add("POST", "/api/chat/threads/<thread_id>/delete", delete_thread)
router.add("POST", "/api/chat/send", send)
