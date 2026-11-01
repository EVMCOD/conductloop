"""Routes for per-agent skill packs (L7, 2026-10-31).

Registered on the shared ``router``. The product-facing API:

* ``GET  /api/skills/packs``                    — the packs that ship with the app
* ``GET  /api/agents/<id>/skills``               — what one agent has installed
* ``POST /api/agents/<id>/skills/install``       — install a pack for that agent
* ``POST /api/agents/<id>/skills/uninstall``     — remove a pack's binding
* ``POST /api/agents/<id>/skills/toggle``        — enable/disable one skill

Local-only, like the rest of /api/learning: these describe the owner's own
agent configuration and never need to be reachable through a tunnel.
"""
from __future__ import annotations

import json
from typing import Any

from .routing import router
from .skills import packs


def _send_json(handler: Any, status: int, obj: dict) -> None:
    handler._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                  "application/json; charset=utf-8")


def _err(handler: Any, status: int, code: str, message: str = "") -> None:
    _send_json(handler, status, {"ok": False, "error": code, "message": message or code})


def _local_only(fn: Any) -> Any:
    """Refuse non-loopback requests. The path parameters the router captured
    (``<agent_id>``) must reach the handler, so **kwargs is forwarded."""
    def wrapper(handler: Any, **kwargs: Any) -> None:
        if not handler._is_local():
            _err(handler, 403, "local_only", "agent skills are only available on this Mac")
            return
        fn(handler, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


MAX_BODY = 64 * 1024


def _body(handler: Any) -> dict | None:
    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
    except (TypeError, ValueError):
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


def _agent_id(value: Any) -> int | None:
    try:
        mid = int(value)
    except (TypeError, ValueError):
        return None
    return mid if mid > 0 else None


@router.get("/api/skills/packs")
@_local_only
def packs_list(handler: Any) -> None:
    """Every pack that ships with the app, with its skills."""
    out = []
    for pack in packs.discover_packs():
        out.append({
            "id": pack["id"],
            "title": pack["title"],
            "role": pack["role"],
            "description": pack["description"],
            "skills": [{"name": s["name"], "description": s["description"]} for s in pack["skills"]],
        })
    _send_json(handler, 200, {"ok": True, "packs": out})


@router.get("/api/agents/<agent_id>/skills")
@_local_only
def agent_skills(handler: Any, agent_id: str) -> None:
    """What one agent has installed, and which packs it carries."""
    mid = _agent_id(agent_id)
    if mid is None:
        _err(handler, 400, "bad_id", "agent id must be a positive integer")
        return
    bound = packs.all_bound_skills(mid)
    installed = packs.installed_packs(mid)
    _send_json(handler, 200, {
        "ok": True,
        "agent_id": mid,
        "skills": bound,
        "packs": installed,
    })


@router.post("/api/agents/<agent_id>/skills/install")
@_local_only
def agent_skills_install(handler: Any, agent_id: str) -> None:
    """Install a pack for one agent. Body: {"pack": "<pack id>"}."""
    mid = _agent_id(agent_id)
    if mid is None:
        _err(handler, 400, "bad_id", "agent id must be a positive integer")
        return
    data = _body(handler)
    if data is None:
        return
    pack_id = data.get("pack")
    if not isinstance(pack_id, str) or not pack_id.strip():
        _err(handler, 400, "bad_request", "pack is required")
        return
    try:
        res = packs.install_pack(pack_id.strip(), agent_id=mid)
    except packs.PackError as e:
        status = 404 if e.code in ("not_found", "no_agent") else 400
        _err(handler, status, e.code, e.message)
        return
    _send_json(handler, 200, res)


@router.post("/api/agents/<agent_id>/skills/uninstall")
@_local_only
def agent_skills_uninstall(handler: Any, agent_id: str) -> None:
    """Remove a pack's binding from one agent. Body: {"pack": "<pack id>"}."""
    mid = _agent_id(agent_id)
    if mid is None:
        _err(handler, 400, "bad_id", "agent id must be a positive integer")
        return
    data = _body(handler)
    if data is None:
        return
    pack_id = data.get("pack")
    if not isinstance(pack_id, str) or not pack_id.strip():
        _err(handler, 400, "bad_request", "pack is required")
        return
    try:
        res = packs.uninstall_pack(pack_id.strip(), agent_id=mid)
    except packs.PackError as e:
        status = 404 if e.code == "not_found" else 400
        _err(handler, status, e.code, e.message)
        return
    _send_json(handler, 200, res)


@router.post("/api/agents/<agent_id>/skills/toggle")
@_local_only
def agent_skills_toggle(handler: Any, agent_id: str) -> None:
    """Enable or disable one bound skill. Body: {"skill": "<key>", "enabled": bool}."""
    mid = _agent_id(agent_id)
    if mid is None:
        _err(handler, 400, "bad_id", "agent id must be a positive integer")
        return
    data = _body(handler)
    if data is None:
        return
    skill_key = data.get("skill")
    enabled = data.get("enabled")
    if not isinstance(skill_key, str) or not skill_key.strip():
        _err(handler, 400, "bad_request", "skill is required")
        return
    if not isinstance(enabled, bool):
        _err(handler, 400, "bad_request", "enabled must be true or false")
        return
    ok = packs.set_skill_enabled(mid, skill_key.strip(), enabled)
    if not ok:
        _err(handler, 404, "not_found", "that skill is not bound to this agent")
        return
    _send_json(handler, 200, {"ok": True, "agent_id": mid, "skill": skill_key.strip(), "enabled": enabled})
