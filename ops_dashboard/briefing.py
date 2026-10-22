"""Morning briefing.

Composes a short daily brief from what ConductLoop already knows — open TODOs,
recent decisions, anomalies, open captures, and today's agenda — and can deliver
it to the Obsidian daily note (and, if configured, chat channels). Wire it to a
schedule (launchd / cron / the app scheduler) to get it every morning.

    from . import briefing
    briefing.deliver()   # compose + write to <vault>/daily/YYYY-MM-DD.md

CLI: `ops briefing`.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from . import db

log = logging.getLogger("conductloop.briefing")

_MARKER = "## 🌅 ConductLoop Briefing"


def _section(title: str, fn) -> str:
    try:
        return fn()
    except Exception as e:
        log.debug("briefing section %s failed: %s", title, e)
        return ""


def compose(today: date | None = None) -> str:
    today = today or date.today()
    lines = [f"{_MARKER} — {today.isoformat()}", ""]

    def _todos():
        s = db.todo_summary()
        return f"- **Open TODOs:** {s.get('open', s.get('total', 0))}"

    def _captures():
        from . import capture
        st = capture.stats()
        return f"- **Inbox to triage:** {st['open']}" if st["open"] else ""

    def _decisions():
        recent = db.list_decisions(limit=3)
        if not recent:
            return ""
        out = ["- **Recent decisions:**"]
        out += [f"  - {d.get('title')}" for d in recent]
        return "\n".join(out)

    def _anomalies():
        from .indexers import anomalies as anom
        a = anom.detect_recent_anomalies()
        hi = [x for x in a.get("anomalies", []) if x.get("severity") in ("high", "medium")]
        if not hi:
            return ""
        out = [f"- **⚠️ Anomalies ({len(hi)}):**"]
        out += [f"  - {x.get('message', x.get('kind'))}" for x in hi[:5]]
        return "\n".join(out)

    def _agenda():
        from . import calendar_sync
        items = calendar_sync.agenda(today)
        if not items:
            return ""
        out = ["- **Today's agenda:**"]
        out += [f"  - {i['time']} {i['title']}" if i.get("time") else f"  - {i['title']}" for i in items]
        return "\n".join(out)

    for title, fn in [("todos", _todos), ("agenda", _agenda), ("anomalies", _anomalies),
                      ("decisions", _decisions), ("captures", _captures)]:
        s = _section(title, fn)
        if s:
            lines.append(s)
    lines.append("")
    lines.append(f"_Generated {datetime.now().isoformat(timespec='minutes')}_")
    return "\n".join(lines)


def to_daily_note(today: date | None = None) -> dict:
    """Write the briefing into <vault>/daily/YYYY-MM-DD.md, replacing any prior
    briefing block for the day (idempotent)."""
    today = today or date.today()
    daily_dir = db.vault_root() / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    note = daily_dir / f"{today.isoformat()}.md"
    brief = compose(today)
    existing = note.read_text(encoding="utf-8") if note.exists() else ""
    # Drop a previous briefing block (from this marker to the next top-level "## ")
    if _MARKER in existing:
        import re
        existing = re.sub(rf"{re.escape(_MARKER)}.*?(?=\n## |\Z)", "", existing, flags=re.S).rstrip() + "\n"
    body = (existing + "\n" + brief + "\n") if existing.strip() else (brief + "\n")
    note.write_text(body, encoding="utf-8")
    return {"note": str(note), "chars": len(brief)}


def deliver(channels: list[str] | None = None, today: date | None = None) -> dict:
    """Compose + write the daily note; optionally push to chat channels."""
    out: dict[str, object] = {"daily_note": to_daily_note(today)}
    brief = compose(today)
    for ch in (channels or []):
        try:
            if ch == "telegram":
                from . import telegram_bridge
                telegram_bridge.broadcast_to_bound_chats(brief)
            elif ch == "discord":
                from . import discord_bridge
                discord_bridge.send_message(brief)
            out[ch] = "sent"
        except Exception as e:
            out[ch] = f"failed: {e}"
    return out
