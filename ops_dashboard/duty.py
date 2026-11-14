"""The standing brief of each agent: what it does when nobody asks.

The C-Suite had a face (the marks of the identity kit) and a playbook (the
skill packs, each skill saying when to use it), and nothing else: the
agents only ever spoke when the person spoke. The triggers that would wake
an agent on its own existed and had never been used once.

This module turns the playbook into a **shift**:

- **The duty** lives in the pack (``ops_dashboard/skills/packs/<role>/pack.json``,
  key ``duty``): when the role wakes up and what it is expected to do. It is
  written from the role's own skills, because the skills already say when
  they apply.
- **The shift** is that duty as a cron trigger on the agent, with
  ``source='shift'`` so the panel can tell "the CTO woke up on its own"
  from "someone ran the CTO".
- **Putting an agent on shift** is the person's decision. The autonomy of
  the Boardroom is tiered and deliberate; an agent that starts spending
  tokens every morning without anyone asking is not a feature, it is a
  surprise.

The wake-up itself carries the pack: the agent does not wake up as a
generic assistant with a role string, it wakes up as the CTO, with its own
criteria.
"""
from __future__ import annotations

from typing import Any

from . import db_agents, db_teams

# The source of a trigger that is the agent's own shift.
SHIFT_SOURCE = "shift"


def duties() -> list[dict[str, Any]]:
    """Every role that has a standing brief."""
    from .skills import packs

    out = []
    for pack in packs.discover_packs():
        duty = pack.get("duty") or {}
        if not duty:
            continue
        out.append({
            "role": pack.get("role") or pack.get("id") or "",
            "pack_id": pack.get("id") or "",
            "title": pack.get("title") or "",
            "description": pack.get("description") or "",
            "cron": duty.get("cron") or "",
            "prompt": duty.get("prompt") or "",
            "skills": [s.get("name") for s in pack.get("skills") or []
                       if isinstance(s, dict)],
        })
    return out


def duty_for(role: str) -> dict[str, Any] | None:
    """The standing brief of one role, by role name."""
    want = (role or "").strip().lower()
    if not want:
        return None
    for duty in duties():
        if duty["role"].lower() == want or duty["pack_id"].lower() == want:
            return duty
    return None


def shift_of(agent_id: int) -> dict[str, Any]:
    """Whether an agent is on shift, and with what.

    Empty when the agent has no shift: ``enabled`` is False and there is
    no trigger.
    """
    duty = duty_for(_role_of(agent_id) or "")
    triggers = [t for t in db_agents.list_triggers(agent_id, enabled_only=False)
                if (t.get("source") or "manual") == SHIFT_SOURCE]
    trigger = triggers[0] if triggers else None
    return {
        "enabled": bool(trigger and trigger.get("enabled")),
        "cron": (trigger or {}).get("spec") or (duty or {}).get("cron") or "",
        "prompt": (trigger or {}).get("prompt") or (duty or {}).get("prompt") or "",
        "trigger_id": (trigger or {}).get("id"),
        "last_fired_at": (trigger or {}).get("last_fired_at"),
        "last_status": (trigger or {}).get("last_status"),
        "last_error": (trigger or {}).get("last_error"),
        "has_duty": duty is not None,
        "role": _role_of(agent_id) or "",
    }


def set_shift(agent_id: int, enabled: bool) -> dict[str, Any]:
    """Put an agent on shift, or take it off.

    Turning it on creates the cron trigger from the role's duty (and
    updates it if the duty changed since). Turning it off disables the
    trigger instead of deleting it: the record of what it did stays, and
    turning it back on is the same button.

    **Turning it off must always work.** The role of an agent can change
    while it is on shift (the CTO becomes a copywriter), and the shift
    keeps firing the old brief until somebody stops it. Asking for a
    duty to turn the shift off would close the switch on the very thing
    that needs it — so a duty is required to turn a shift *on*, and never
    to turn it off. (The bot's finding on PR #141.)
    """
    role = _role_of(agent_id) or ""
    existing = [t for t in db_agents.list_triggers(agent_id, enabled_only=False)
                if (t.get("source") or "manual") == SHIFT_SOURCE]
    if enabled:
        if not role:
            raise ValueError("no_agent")
        duty = duty_for(role)
        if not duty:
            raise ValueError("no_duty_for_role")
        if existing:
            trigger_id = int(existing[0]["id"])
            db_agents.update_trigger(trigger_id, spec=duty["cron"],
                                     prompt=duty["prompt"], enabled=True)
        else:
            trigger_id = db_agents.add_trigger(
                agent_id, "cron", duty["cron"], prompt=duty["prompt"],
                enabled=True, source=SHIFT_SOURCE)
    else:
        trigger_id = int(existing[0]["id"]) if existing else 0
        if existing:
            db_agents.update_trigger(trigger_id, enabled=False)
    return shift_of(agent_id)


def _role_of(agent_id: int) -> str:
    """The role of an agent, as the packs name it."""
    try:
        member = db_teams.get_member(agent_id)
    except Exception:
        return ""
    if not member:
        return ""
    return str(member.get("role") or "").strip()
