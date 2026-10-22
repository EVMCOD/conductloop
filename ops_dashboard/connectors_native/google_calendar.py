"""Google Calendar connector — read + ingest, and writes behind confirmation.

Scopes (minimal, read): ``calendar.calendarlist.readonly`` (which calendars you
have) and ``calendar.events.readonly`` (their events). Nothing else is ever
requested in read mode.

Actions:
* ``list_calendars`` (read) — your calendar list.
* ``list_events`` (read) — events of one calendar in a window.
* ``sync`` (read) — fetch the selected calendars' events in a window
  (default 7 days back, 30 ahead) and ingest them locally:
  - ``calendar_events`` (uid ``gcal:<calendar>:<event>``, source
    ``google_calendar``) so the morning briefing's agenda shows them;
  - the vault (``documents``, source ``google_calendar``) so the second brain
    can search titles/descriptions. Attendee e-mails are **not** stored, only
    a count. Cancelled / vanished events in the window are pruned.

Writes (only with permission ``write``, which adds ``calendar.events.owned``:
events on calendars you own — not your calendar list, settings or ACLs). Each
one is queued by ``connector_actions`` and runs only after you confirm the
exact payload on /connectors (15-minute expiry):
* ``create_event`` — title, start/end (RFC 3339 with offset, or YYYY-MM-DD for
  all-day), optional location/description.
* ``update_event`` — patch those fields of one event.
* ``delete_event`` — delete one event.
No attendees field on purpose: nothing here can e-mail invitations to other
people, and every call sends ``sendUpdates=none``. Changes show up locally on
the next sync.
"""
from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from .. import connector_actions as actions
from .. import connector_registry as registry
from .. import connector_store as store
from . import _google, _ingest

ID = "google_calendar"
API = "https://www.googleapis.com/calendar/v3"
READ_SCOPES = (_google.scope("calendar.calendarlist.readonly"), _google.scope("calendar.events.readonly"))
WRITE_SCOPES = (_google.scope("calendar.events.owned"),)
MAX_CALENDARS = 20
EVENT_FIELDS = ("title", "start", "end", "location", "description")
MAX_DURATION = 14 * 86400


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_start(ev: dict[str, Any]) -> tuple[int, bool] | None:
    st = ev.get("start") or {}
    try:
        if st.get("dateTime"):
            return int(datetime.fromisoformat(str(st["dateTime"]).replace("Z", "+00:00")).timestamp()), False
        if st.get("date"):
            d = datetime.strptime(str(st["date"]), "%Y-%m-%d").replace(tzinfo=UTC)
            return int(d.timestamp()), True
    except ValueError:
        return None
    return None


def _updated(ev: dict[str, Any]) -> int | None:
    try:
        return int(datetime.fromisoformat(str(ev.get("updated", "")).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _calendars(client: Any) -> list[dict[str, Any]]:
    items = _google.paged(client, f"{API}/users/me/calendarList", {"maxResults": 250})
    return [{"id": c.get("id"), "summary": c.get("summaryOverride") or c.get("summary"),
             "primary": bool(c.get("primary")), "selected": bool(c.get("selected") or c.get("primary")),
             "time_zone": c.get("timeZone"), "access_role": c.get("accessRole")}
            for c in items if c.get("id")]


def _events(client: Any, cal_id: str, start: float, end: float) -> list[dict[str, Any]]:
    return _events_ex(client, cal_id, start, end)[0]


def _events_ex(client: Any, cal_id: str, start: float, end: float) -> tuple[list[dict[str, Any]], bool]:
    return _google.paged_ex(client, f"{API}/calendars/{quote(cal_id, safe='')}/events",
                            {"timeMin": _rfc3339(start), "timeMax": _rfc3339(end), "singleEvents": "true",
                             "orderBy": "startTime", "maxResults": 250, "showDeleted": "false"})


def list_calendars(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"calendars": _calendars(client)}


def list_events(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    cal = str(payload.get("calendar_id") or "primary")
    now = time.time()
    back = _ingest.clamp_int(payload, "days_back", 0, 0, 90)
    ahead = _ingest.clamp_int(payload, "days_ahead", 7, 1, 365)
    evs = _events(client, cal, now - back * 86400, now + ahead * 86400)
    out = []
    for ev in evs:
        st = _parse_start(ev)
        if st is None or ev.get("status") == "cancelled":
            continue
        out.append({"id": ev.get("id"), "title": ev.get("summary") or "(sin título)", "starts_at": st[0],
                    "all_day": st[1], "location": ev.get("location")})
    return {"calendar_id": cal, "events": out}


def _doc(cal: dict[str, Any], ev: dict[str, Any], starts_at: int, all_day: bool) -> str:
    when = datetime.fromtimestamp(starts_at, tz=UTC).strftime("%Y-%m-%d" if all_day else "%Y-%m-%d %H:%M UTC")
    lines = [f"# {ev.get('summary') or '(sin título)'}", "", f"- Cuándo: {when}",
             f"- Calendario: {cal.get('summary') or cal.get('id')}"]
    if ev.get("location"):
        lines.append(f"- Lugar: {ev['location']}")
    attendees = ev.get("attendees") or []
    if attendees:
        lines.append(f"- Asistentes: {len(attendees)}")
    if ev.get("description"):
        lines += ["", str(ev["description"])]
    return "\n".join(lines)


def sync(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    back = _ingest.clamp_int(payload, "days_back", 7, 0, 90)
    ahead = _ingest.clamp_int(payload, "days_ahead", 30, 1, 365)
    start, end = now - back * 86400, now + ahead * 86400
    cals = [c for c in _calendars(client) if c["selected"]]
    wanted = payload.get("calendar_ids")
    if isinstance(wanted, list) and wanted:
        cals = [c for c in cals if c["id"] in {str(w) for w in wanted}]
    cals = cals[:MAX_CALENDARS]
    primary = next((c for c in cals if c["primary"]), None)
    if primary and isinstance(primary["id"], str) and "@" in primary["id"]:
        store.upsert_account(ID, account_label=primary["id"][:120])   # primary calendar id = your address
    stats = {"calendars": len(cals), "events": 0, "vault_updated": 0, "pruned": 0, "truncated": 0}
    for cal in cals:
        uid_prefix = f"gcal:{cal['id']}:"
        keep_uids: set[str] = set()
        keep_paths: set[str] = set()
        events, complete = _events_ex(client, cal["id"], start, end)
        for ev in events:
            if ev.get("status") == "cancelled" or not ev.get("id"):
                continue
            st = _parse_start(ev)
            if st is None:
                continue
            uid = uid_prefix + str(ev["id"])
            path = f"google_calendar/{cal['id']}/{ev['id']}"
            title = str(ev.get("summary") or "(sin título)")[:300]
            _ingest.upsert_calendar_event(uid=uid, title=title, starts_at=st[0], all_day=st[1], source=ID)
            if _ingest.to_vault(source=ID, path=path, title=title, content=_doc(cal, ev, st[0], st[1]),
                                metadata={"kind": "event", "calendar": cal.get("summary"), "starts_at": st[0],
                                          "all_day": st[1], "tags": ["calendar", "google"]},
                                mtime=_updated(ev)):
                stats["vault_updated"] += 1
            keep_uids.add(uid)
            keep_paths.add(path)
            stats["events"] += 1
        if not complete:
            # More pages than we read: absence from a partial listing proves nothing.
            stats["truncated"] += 1
            continue
        stats["pruned"] += _ingest.prune_calendar(ID, uid_prefix, int(start), int(end), keep_uids)
        # Cancelled/deleted events leave the vault too, within the same window.
        stats["pruned"] += _ingest.prune_window(ID, f"google_calendar/{cal['id']}/", keep_paths,
                                                int(start), int(end))
    return stats


# ---------- writes (pending until confirmed) ----------

def _bad(msg: str) -> actions.ActionError:
    return actions.ActionError("bad_payload", msg, 400)


def _text(payload: dict[str, Any], key: str, limit: int, *, required: bool = False, oneline: bool = True) -> str | None:
    v = payload.get(key)
    if v is None or v == "":
        if required:
            raise _bad(f"{key} is required")
        return None
    if not isinstance(v, str) or len(v) > limit:
        raise _bad(f"{key} must be a string <= {limit} chars")
    bad = "\r\n\x00" if oneline else "\x00"
    if any(c in v for c in bad):
        raise _bad(f"{key} has control characters")
    return v.strip()


def _calendar_id(payload: dict[str, Any]) -> str:
    v = payload.get("calendar_id", "primary")
    if not isinstance(v, str) or not re.fullmatch(r"[^\s/]{1,200}", v):
        raise _bad("calendar_id is invalid")
    return v


def _event_id(payload: dict[str, Any]) -> str:
    v = payload.get("event_id")
    if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,1024}", v):
        raise _bad("event_id is invalid")
    return v


def _when(v: Any, key: str) -> tuple[dict[str, str], int, bool]:
    if not isinstance(v, str):
        raise _bad(f"{key} must be RFC 3339 with offset or YYYY-MM-DD")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        try:
            d = datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError as e:
            raise _bad(f"{key} is not a date") from e
        return {"date": v}, int(d.timestamp()), True
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as e:
        raise _bad(f"{key} must be RFC 3339 with offset or YYYY-MM-DD") from e
    if dt.tzinfo is None:
        raise _bad(f"{key} needs a UTC offset (e.g. +02:00)")
    return {"dateTime": dt.isoformat()}, int(dt.timestamp()), False


def _event_body(payload: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    allowed = {"calendar_id", "event_id", *EVENT_FIELDS} if partial else {"calendar_id", *EVENT_FIELDS}
    extra = sorted(set(payload) - allowed)
    if extra:
        raise _bad(f"unsupported fields: {', '.join(extra)} (no attendees/invitations)")
    body: dict[str, Any] = {}
    title = _text(payload, "title", 300, required=not partial)
    if title is not None:
        body["summary"] = title
    for key, limit, one in (("location", 300, True), ("description", 5000, False)):
        if partial and payload.get(key) == "":
            body[key] = ""          # explicit "" clears it (PATCH leaves omitted fields alone)
            continue
        v = _text(payload, key, limit, oneline=one)
        if v is not None:
            body[key] = v
    has_s, has_e = payload.get("start") is not None, payload.get("end") is not None
    if not partial or has_s or has_e:
        if not (has_s and has_e):
            raise _bad("start and end go together")
        s, s_ts, s_day = _when(payload["start"], "start")
        e, e_ts, e_day = _when(payload["end"], "end")
        if s_day != e_day:
            raise _bad("start and end must both be dates or both date-times")
        if e_ts <= s_ts or e_ts - s_ts > MAX_DURATION:
            raise _bad("end must be after start (at most 14 days)")
        body["start"], body["end"] = s, e
    if partial and not body:
        raise _bad("nothing to change")
    return body


def _when_text(body: dict[str, Any]) -> str:
    if "start" not in body:
        return ""
    s, e = body["start"], body["end"]
    return f" · {s.get('date') or s.get('dateTime')} → {e.get('date') or e.get('dateTime')}"


def _summary_create(payload: dict[str, Any]) -> str:
    body = _event_body(payload, partial=False)
    loc = f" · lugar: {body['location']}" if body.get("location") else ""
    return (f"Google Calendar · crear evento «{body['summary']}»{_when_text(body)}{loc}"
            f" · calendario {_calendar_id(payload)} · sin invitaciones")


def _summary_update(payload: dict[str, Any]) -> str:
    body = _event_body(payload, partial=True)
    changes = ", ".join(k if k != "summary" else "title" for k in body if k not in ("end",))
    title = f" → «{body['summary']}»" if "summary" in body else ""
    return (f"Google Calendar · cambiar evento {_event_id(payload)} ({changes}){title}{_when_text(body)}"
            f" · calendario {_calendar_id(payload)} · sin avisar a asistentes")


def _summary_delete(payload: dict[str, Any]) -> str:
    extra = sorted(set(payload) - {"calendar_id", "event_id"})
    if extra:
        raise _bad(f"unsupported fields: {', '.join(extra)}")
    return (f"Google Calendar · BORRAR evento {_event_id(payload)} · calendario {_calendar_id(payload)}"
            " · sin avisar a asistentes")


def _ev_url(payload: dict[str, Any], event_id: str | None = None) -> str:
    base = f"{API}/calendars/{quote(_calendar_id(payload), safe='')}/events"
    return f"{base}/{quote(event_id, safe='')}" if event_id else base


def _result(ev: dict[str, Any]) -> dict[str, Any]:
    return {"id": ev.get("id"), "status": ev.get("status"), "link": ev.get("htmlLink"),
            "note": "se verá en la agenda y el vault en la próxima sincronización"}


def create_event(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    body = _event_body(payload, partial=False)
    return _result(_google.send_json(client, "POST", _ev_url(payload), params={"sendUpdates": "none"}, body=body))


def update_event(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    body = _event_body(payload, partial=True)
    url = _ev_url(payload, _event_id(payload))
    return _result(_google.send_json(client, "PATCH", url, params={"sendUpdates": "none"}, body=body))


def delete_event(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    _summary_delete(payload)
    _google.send_json(client, "DELETE", _ev_url(payload, _event_id(payload)), params={"sendUpdates": "none"})
    return {"id": payload["event_id"], "status": "deleted",
            "note": "desaparecerá de la agenda y el vault en la próxima sincronización"}


SPEC = registry.register(registry.ConnectorSpec(
    id=ID, name="Google Calendar",
    allowed_hosts=(*_google.AUTH_HOSTS, "www.googleapis.com"),
    read_scopes=READ_SCOPES,
    write_scopes=WRITE_SCOPES,
    actions={
        "list_calendars": registry.ActionSpec("list_calendars", "read", "List your calendars", list_calendars),
        "list_events": registry.ActionSpec("list_events", "read", "List events of a calendar", list_events),
        "sync": registry.ActionSpec("sync", "read", "Import events into the agenda and the vault", sync),
        "create_event": registry.ActionSpec("create_event", "write", "Create an event (no invitations)",
                                            create_event, _summary_create),
        "update_event": registry.ActionSpec("update_event", "write", "Change an event (attendees not notified)",
                                            update_event, _summary_update),
        "delete_event": registry.ActionSpec("delete_event", "write", "Delete an event (attendees not notified)",
                                            delete_event, _summary_delete),
    },
    **_google.oauth_kwargs(),
))
_google.join_group(ID)
