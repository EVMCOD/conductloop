"""The standing brief of each agent, and putting an agent on shift.

GET  /api/agents/duties        every role that has a brief, and who is on shift
POST /api/agents/<id>/shift    put an agent on shift, or take it off

An agent on shift wakes up on its own with the brief of its role and its
own skills. Turning it on is the person's decision: an agent that starts
working every morning without anyone asking is not a feature.

Local only: putting an agent on shift makes it spend tokens on this Mac.
"""
from __future__ import annotations

from collections.abc import Callable

from . import duty
from .routes_learning import _body, _err, _send_json
from .routing import router


def _local_only(fn: Callable) -> Callable:
    def wrapper(handler, *args, **kwargs) -> None:
        if not handler._is_local():
            _err(handler, 403, "local_only",
                 "the agents only work on this Mac")
            return
        fn(handler, *args, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _members() -> list[dict]:
    """The agents of the C-Suite and their shift.

    An agent whose role changed while it was on shift keeps its row even
    when its new role has no brief: the shift is still firing the old
    one, and the person has to be able to see it and turn it off.
    """
    from . import db_teams

    try:
        members = db_teams.list_members()
    except Exception:
        members = []
    out = []
    for m in members:
        if m.get("type") != "ai_agent":
            continue
        role = str(m.get("role") or "")
        brief = duty.duty_for(role)
        try:
            shift = duty.shift_of(int(m["id"]))
        except Exception:
            shift = {"enabled": False}
        # No brief and no shift: there is nothing to say about this agent.
        if not brief and not shift.get("enabled") and not shift.get("trigger_id"):
            continue
        out.append({
            "id": m.get("id"),
            "name": m.get("name") or "",
            "role": role,
            "mark": m.get("mark") or "",
            # What it wakes up on: the shift that is on, or the brief its
            # role would have.
            "cron": (shift.get("cron") or (brief or {}).get("cron") or ""),
            "prompt": (shift.get("prompt") or (brief or {}).get("prompt") or ""),
            "on_shift": bool(shift.get("enabled")),
            # False when the role changed and the brief it is running is
            # the old one: the panel says so instead of hiding it.
            "has_duty": bool(brief),
            "last_fired_at": shift.get("last_fired_at"),
            "last_status": shift.get("last_status"),
            "last_error": shift.get("last_error"),
        })
    return out


@router.get("/api/agents/duties")
@_local_only
def _duties(handler) -> None:
    """Every role with a standing brief, and who is on shift."""
    _send_json(handler, 200, {
        "ok": True,
        "duties": duty.duties(),
        "agents": _members(),
    })


@router.post("/api/agents/<agent_id>/shift")
@_local_only
def _shift(handler, agent_id: str = "") -> None:
    """Put an agent on shift, or take it off."""
    try:
        agent = int(agent_id)
    except (TypeError, ValueError):
        _err(handler, 400, "bad_agent", "not an agent")
        return
    body = _body(handler)
    if body is None:
        return
    enabled = bool(body.get("enabled", True))
    try:
        shift = duty.set_shift(agent, enabled)
    except ValueError as e:
        code = str(e)
        if code == "no_agent":
            _err(handler, 404, "no_agent", "there is no agent with that id")
        elif code == "no_duty_for_role":
            _err(handler, 404, "no_duty",
                 "this agent's role has no standing brief")
        else:
            _err(handler, 500, "shift_failed", code)
        return
    except Exception as e:  # pragma: no cover - defensive
        _err(handler, 500, "shift_failed", str(e))
        return
    _send_json(handler, 200, {"ok": True, "shift": shift})
