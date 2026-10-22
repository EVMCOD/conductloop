"""Calendar import (ICS) + agenda.

Imports events from an iCalendar (.ics) file or URL — as exported by Google
Calendar, Apple Calendar, etc. — into ``calendar_events`` so the morning
briefing can surface today's agenda. Minimal, dependency-free VEVENT parser:
handles SUMMARY + DTSTART (date and datetime forms).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import UTC, date, datetime

from . import db

log = logging.getLogger("conductloop.calendar")

_UNFOLD = re.compile(r"\r?\n[ \t]")  # ICS line folding


def _parse_dt(val: str) -> tuple[int, bool]:
    """Return (epoch_seconds, all_day). Accepts YYYYMMDD or YYYYMMDDT HHMMSS[Z]."""
    val = val.strip()
    if "T" in val:
        fmt = "%Y%m%dT%H%M%SZ" if val.endswith("Z") else "%Y%m%dT%H%M%S"
        dt = datetime.strptime(val, fmt)
        if val.endswith("Z"):
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp()), False
    dt = datetime.strptime(val, "%Y%m%d").replace(tzinfo=UTC)
    return int(dt.timestamp()), True


def parse_ics(text: str) -> list[dict]:
    text = _UNFOLD.sub("", text)  # unfold continued lines
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        fields = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            fields[key.split(";")[0].strip().upper()] = val.strip()
        if "DTSTART" not in fields or "SUMMARY" not in fields:
            continue
        try:
            starts_at, all_day = _parse_dt(fields["DTSTART"])
        except Exception:
            continue
        events.append({
            "uid": fields.get("UID"),
            "title": fields["SUMMARY"],
            "starts_at": starts_at,
            "all_day": all_day,
        })
    return events


def import_ics(source: str) -> dict:
    """Import events from an .ics file path or http(s) URL. Upserts by UID."""
    if source.startswith("http://") or source.startswith("https://"):
        import httpx
        text = httpx.get(source, timeout=15, follow_redirects=True).text
    else:
        from pathlib import Path
        text = Path(source).expanduser().read_text(encoding="utf-8")
    events = parse_ics(text)
    now = int(time.time())
    with db.get_connection() as conn:
        for e in events:
            conn.execute(
                "INSERT INTO calendar_events (uid, title, starts_at, all_day, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(uid) DO UPDATE SET title=excluded.title, "
                "starts_at=excluded.starts_at, all_day=excluded.all_day",
                (e["uid"], e["title"], e["starts_at"], 1 if e["all_day"] else 0, source, now),
            )
        conn.commit()
    log.info("imported %d calendar events from %s", len(events), source)
    return {"imported": len(events), "source": source}


def agenda(day: date | None = None) -> list[dict]:
    """Events for the given day (default today), ordered by start time."""
    day = day or date.today()
    start = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
    end = start + 86400
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM calendar_events WHERE starts_at >= ? AND starts_at < ? ORDER BY starts_at",
            (start, end),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        t = "" if d["all_day"] else datetime.fromtimestamp(d["starts_at"], tz=UTC).strftime("%H:%M")
        out.append({"title": d["title"], "time": t, "all_day": bool(d["all_day"])})
    return out
