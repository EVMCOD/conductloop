"""In-app browser routes (browser design, PRs 4+).

PR 4 only serves the ``/browser`` page (tab strip + URL bar). The tabs
themselves are native child webviews driven by the desktop app's
``browser_*`` commands; nothing here talks to them.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .routing import router

_WEB = Path(__file__).resolve().parent / "web"


@router.get("/browser")
def browser_page(handler: Any) -> None:
    handler._send(200, (_WEB / "browser.html").read_bytes(), "text/html; charset=utf-8")


MAX_CLIP_BODY = 4 * 1024 * 1024


@router.post("/api/web/clip")
def web_clip(handler: Any) -> None:
    """Body: the object returned by the desktop app's browser_extract."""
    import json

    from . import web_clip as wc

    def reply(status: int, obj: dict) -> None:
        handler._send(status, json.dumps(obj).encode(), "application/json")

    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = -1
    if length <= 0 or length > MAX_CLIP_BODY:
        reply(413 if length > MAX_CLIP_BODY else 400, {"ok": False, "error": "bad_size"})
        return
    try:
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
        reply(200, wc.save_clip(payload))
    except wc.ClipError as e:
        reply(400, {"ok": False, "error": e.code, "message": str(e)})
    except (ValueError, UnicodeDecodeError):
        reply(400, {"ok": False, "error": "bad_json"})


@router.post("/api/brain/ask-page")
def brain_ask_page(handler: Any) -> None:
    """Body: {question, page: <browser_extract result>, provider?}. No tools,
    no memory, page wrapped as untrusted (see web_ask)."""
    import json

    from . import brain, web_ask
    from .web_clip import ClipError

    def reply(status: int, obj: dict) -> None:
        handler._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = -1
    if length <= 0 or length > MAX_CLIP_BODY:
        reply(413 if length > MAX_CLIP_BODY else 400, {"ok": False, "error": "bad_size"})
        return
    try:
        data = json.loads(handler.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        provider = data.get("provider") if isinstance(data.get("provider"), str) else None
        reply(200, web_ask.ask_page(data.get("question"), data.get("page"), provider_name=provider))
    except ClipError as e:
        reply(400, {"ok": False, "error": e.code, "message": str(e)})
    except brain.ProviderError as e:
        reply(e.status or 502, {"ok": False, "error": e.code, "message": e.message})
    except (ValueError, UnicodeDecodeError):
        reply(400, {"ok": False, "error": "bad_json"})


# ── Agent browser sessions (browser design PR 8) ─────────────────────────────
# Registered through the sessions API wrapper (same origin check, error
# mapping and secret redaction as /api/sessions/*). Browser sessions have no
# repo; tasks, start/stop/resume/close and events reuse /api/sessions/*.

from . import routes_sessions as _rs  # noqa: E402


@_rs.route("POST", "/api/sessions/browser")
def create_browser_session(handler: Any) -> tuple[int, dict]:
    from .sessions import browser as sb

    data = _rs._body(handler)
    provider = (_rs._str(data, "provider", max_len=40) or "").lower()
    if provider not in _rs.KNOWN_PROVIDERS:
        raise _rs.ApiError(400, "validation_error", "provider: unknown", field="provider")
    mode = _rs._str(data, "browser_mode", required=False, max_len=10) or "read"
    if mode not in ("read", "act"):
        raise _rs.ApiError(400, "validation_error", "browser_mode: 'read' or 'act'",
                           field="browser_mode")
    kwargs: dict[str, Any] = {}
    if data.get("max_iterations") is not None:
        kwargs["max_iterations"] = _rs._int(data["max_iterations"], "max_iterations")
    st = _rs._store()
    role = _rs._str(data, "role", required=False, max_len=60) or "browser"
    sid = sb.create_browser_session(
        st, name=_rs._str(data, "name", required=False, max_len=80) or "Browser agent",
        role=role, provider=provider, model=_rs._str(data, "model", max_len=200) or "",
        budget_cost_cents=_rs._int(data.get("budget_cost_cents"), "budget_cost_cents", minimum=1),
        browser_mode=mode,
        key_ref=_rs._str(data, "key_ref", required=provider in _rs.KEYED_PROVIDERS, max_len=200),
        **kwargs)
    out: dict[str, Any] = {"ok": True}
    prompt = _rs._str(data, "prompt", required=False, max_len=20_000)
    if prompt:
        tid = st.enqueue_task(title=(_rs._str(data, "title", required=False, max_len=200)
                                     or prompt[:80]), prompt=prompt, assigned_session_id=sid)
        out["task"] = _rs._public_task(st.get_task(tid) or {})
    out["session"] = _rs._public_session(st.get_session(sid) or {})
    out["bridge_available"] = sb.BrowserBridge().available
    return 201, out


@_rs.route("POST", "/api/sessions/<sid>/browser-mode")
def set_browser_mode(handler: Any, sid: str) -> tuple[int, dict]:
    from .sessions import browser as sb

    s = _rs._session_or_404(sid)
    data = _rs._body(handler)
    mode = _rs._str(data, "browser_mode", max_len=10) or ""
    if mode not in ("read", "act"):
        raise _rs.ApiError(400, "validation_error", "browser_mode: 'read' or 'act'",
                           field="browser_mode")
    if not sb.set_browser_mode(_rs._store(), s["id"], mode):
        raise _rs.ApiError(409, "invalid_state", "not a browser session")
    return 200, {"ok": True, "session": _rs._public_session(_rs._store().get_session(s["id"]) or {})}


# PR 9: the agent bar's Approve / Reject for the agent's click/type. Only
# pending approvals of THAT session; the generic /api/approvals also lists
# them. An approval is single use and bound to the DOM it was asked on.

@_rs.route("GET", "/api/sessions/<sid>/browser-approvals")
def list_browser_approvals(handler: Any, sid: str) -> tuple[int, dict]:
    from .sessions import browser as sb

    s = _rs._session_or_404(sid)
    return 200, {"ok": True, "approvals": sb.pending_browser_approvals(_rs._store(), s["id"])}


@_rs.route("POST", "/api/sessions/<sid>/browser-approvals")
def decide_browser_approval(handler: Any, sid: str) -> tuple[int, dict]:
    from .sessions import browser as sb

    s = _rs._session_or_404(sid)
    data = _rs._body(handler)
    decision = _rs._str(data, "decision", max_len=10) or ""
    if decision not in ("approve", "reject"):
        raise _rs.ApiError(400, "validation_error", "decision: 'approve' or 'reject'",
                           field="decision")
    aid = _rs._int(data.get("id"), "id", minimum=1)
    if not sb.resolve_browser_approval(_rs._store(), aid, s["id"], decision):
        raise _rs.ApiError(409, "invalid_state", "no pending approval with that id for this session")
    return 200, {"ok": True, "decision": decision}
