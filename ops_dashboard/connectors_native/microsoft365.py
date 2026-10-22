"""Microsoft 365 (Outlook mail + calendar) — read + ingest, and sending mail /
changing events behind confirmation.

OAuth: Microsoft identity platform v2, endpoint ``common`` (work/school and
personal accounts), public client with PKCE — no client secret (Entra
"Mobile and desktop applications" platform). Redirect
``http://localhost:<free port>/callback``: Entra ignores the port on localhost
redirects, so the app registers ``http://localhost/callback`` once
(``redirect_host="localhost"``; the receiver still binds 127.0.0.1).

Scopes (read): ``offline_access`` + Graph ``User.Read Mail.Read
Calendars.Read``, requested as full Graph URIs (Entra echoes them that way in
the token response, so granted == requested). Nothing else.

Actions:
* ``list_messages`` / ``get_message`` (read) — Inbox, bodies as text
  (``Prefer: outlook.body-content-type="text"``); attachments never fetched.
* ``list_events`` (read) — calendar view in a window.
* ``sync`` (read) — Inbox mail of the last N days (default 3, max 50) into
  the vault (source ``microsoft365``) and calendar events (default 7 days back,
  30 ahead) into ``calendar_events`` (uid ``m365:<id>``) + the vault; events
  that vanished from the window are pruned. Attendee addresses are not stored.

Writes (only with permission ``write``, which adds Graph ``Mail.Send`` and
``Calendars.ReadWrite``; each one queued and run only after you confirm the
exact payload on /connectors, 15-minute expiry):
* ``send_mail`` — plain-text mail to 1–10 plain addresses (To + Cc, no Bcc,
  no attachments, no HTML), saved to Sent Items. Same validation as Gmail.
* ``create_event`` — title, start/end (RFC 3339 with offset, or YYYY-MM-DD
  for all-day), optional location/description. No attendees field: nothing
  sends invitations.
* ``update_event`` / ``delete_event`` — only events **without attendees**
  (checked against Outlook right before running), because Outlook itself
  e-mails updates/cancellations to attendees and that cannot be turned off.

Revocation: no programmatic revoke in v2 for a single refresh token —
disconnecting deletes the token locally; revoke at
https://myaccount.microsoft.com/ (or account.live.com/consent/Manage).
"""
from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .. import connector_actions as actions
from .. import connector_registry as registry
from .. import connector_store as store
from . import _ingest
from .gmail import _send_fields
from .google_calendar import _text, _when

ID = "microsoft365"
AUTH = "https://login.microsoftonline.com/common/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
G = "https://graph.microsoft.com/"
READ_SCOPES = ("offline_access", G + "User.Read", G + "Mail.Read", G + "Calendars.Read")
WRITE_SCOPES = (G + "Mail.Send", G + "Calendars.ReadWrite")
TEXT_BODY = {"Prefer": 'outlook.body-content-type="text"'}
MAX_MAIL = 50
MAX_PAGES = 10
_ID_RE = re.compile(r"[A-Za-z0-9_\-=+/]{1,512}")


class GraphError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"graph_{status}:{code}")
        self.status, self.code = status, code


def get_json(client: Any, url: str, params: dict[str, Any] | None = None,
             headers: dict[str, str] | None = None) -> dict[str, Any]:
    if urlparse(url).hostname != "graph.microsoft.com":
        raise GraphError(400, "host_not_graph")
    r = client.get(url, params=params, headers=headers or {})
    if r.status_code >= 400:
        code = ""
        try:
            err = r.json().get("error")
            code = str(err.get("code") if isinstance(err, dict) else err or "")
        except (ValueError, AttributeError):
            pass
        raise GraphError(r.status_code, code[:60])
    body = r.json()
    if not isinstance(body, dict):
        raise GraphError(502, "not_an_object")
    return body


def paged(client: Any, url: str, params: dict[str, Any], limit: int,
          headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    next_url: str | None = url
    p: dict[str, Any] | None = params
    for _ in range(MAX_PAGES):
        if not next_url:
            break
        body = get_json(client, next_url, p, headers)
        out += [x for x in body.get("value") or [] if isinstance(x, dict)]
        if len(out) >= limit:
            break
        next_url, p = body.get("@odata.nextLink"), None        # nextLink already carries the query
    return out[:limit]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s: Any) -> int | None:
    if not s:
        return None
    v = str(s).replace("Z", "+00:00")
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)                     # Graph sends 7 fractional digits
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp())


def _safe_id(v: Any) -> str:
    s = str(v or "")
    if not _ID_RE.fullmatch(s):
        raise ValueError("bad id")
    return s


def _addr(x: Any) -> str:
    ea = (x or {}).get("emailAddress") or {}
    name, mail = ea.get("name") or "", ea.get("address") or ""
    return f"{name} <{mail}>" if name and mail and name != mail else (mail or name)


_MSG_SELECT = "id,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,body,hasAttachments,webLink"


def parse_message(m: dict[str, Any]) -> dict[str, Any]:
    body = m.get("body") or {}
    text = str(body.get("content") or m.get("bodyPreview") or "")
    if (body.get("contentType") or "").lower() == "html":
        from .gmail import html_to_text  # same sanitiser as Gmail (scripts/styles stripped)
        text = html_to_text(text)
    return {"id": m.get("id"), "subject": m.get("subject") or "(sin asunto)", "from": _addr(m.get("from")),
            "to": ", ".join(_addr(r) for r in m.get("toRecipients") or []),
            "cc": ", ".join(_addr(r) for r in m.get("ccRecipients") or []),
            "received": _ts(m.get("receivedDateTime")), "has_attachments": bool(m.get("hasAttachments")),
            "link": m.get("webLink"), "text": text.strip()}


def _inbox(client: Any, since: float | None, limit: int, *, full: bool) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"$top": min(50, limit), "$orderby": "receivedDateTime desc",
                              "$select": _MSG_SELECT if full else "id,subject,from,receivedDateTime,bodyPreview,webLink"}
    if since:
        params["$filter"] = f"receivedDateTime ge {_iso(since)}"
    return paged(client, f"{GRAPH}/me/mailFolders/inbox/messages", params, limit, TEXT_BODY if full else None)


def list_messages(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    limit = _ingest.clamp_int(payload, "max", 20, 1, 50)
    return {"messages": [{"id": m.get("id"), "subject": m.get("subject"), "from": _addr(m.get("from")),
                          "received": _ts(m.get("receivedDateTime")), "snippet": m.get("bodyPreview")}
                         for m in _inbox(client, None, limit, full=False)]}


def get_message(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    mid = _safe_id(payload.get("id"))
    m = get_json(client, f"{GRAPH}/me/messages/{mid}", {"$select": _MSG_SELECT}, TEXT_BODY)
    p = parse_message(m)
    p["text"] = p["text"][:50000]
    return p


def _events(client: Any, start: float, end: float) -> list[dict[str, Any]]:
    return paged(client, f"{GRAPH}/me/calendarView",
                 {"startDateTime": _iso(start), "endDateTime": _iso(end), "$top": 100, "$orderby": "start/dateTime",
                  "$select": "id,subject,start,end,isAllDay,isCancelled,location,bodyPreview,attendees,webLink,"
                             "lastModifiedDateTime,organizer"},
                 1000, {"Prefer": 'outlook.timezone="UTC"'})


def _event_start(ev: dict[str, Any]) -> int | None:
    return _ts((ev.get("start") or {}).get("dateTime"))


def list_events(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    back = _ingest.clamp_int(payload, "days_back", 0, 0, 90)
    ahead = _ingest.clamp_int(payload, "days_ahead", 7, 1, 365)
    out = []
    for ev in _events(client, now - back * 86400, now + ahead * 86400):
        st = _event_start(ev)
        if st is None or ev.get("isCancelled"):
            continue
        out.append({"id": ev.get("id"), "title": ev.get("subject") or "(sin título)", "starts_at": st,
                    "all_day": bool(ev.get("isAllDay")), "location": (ev.get("location") or {}).get("displayName")})
    return {"events": out}


def _mail_doc(p: dict[str, Any]) -> str:
    lines = [f"# {p['subject']}", "", f"- De: {p['from']}", f"- Para: {p['to']}"]
    if p["cc"]:
        lines.append(f"- Cc: {p['cc']}")
    if p["received"]:
        lines.append(f"- Fecha: {datetime.fromtimestamp(p['received'], tz=UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    if p["has_attachments"]:
        lines.append("- Adjuntos: sí (no descargados)")
    return "\n".join(lines) + "\n\n" + p["text"]


def _event_doc(ev: dict[str, Any], st: int) -> str:
    when = datetime.fromtimestamp(st, tz=UTC).strftime("%Y-%m-%d" if ev.get("isAllDay") else "%Y-%m-%d %H:%M UTC")
    lines = [f"# {ev.get('subject') or '(sin título)'}", "", f"- Cuándo: {when}"]
    loc = (ev.get("location") or {}).get("displayName")
    if loc:
        lines.append(f"- Lugar: {loc}")
    if ev.get("attendees"):
        lines.append(f"- Asistentes: {len(ev['attendees'])}")
    if ev.get("bodyPreview"):
        lines += ["", str(ev["bodyPreview"])]
    return "\n".join(lines)


def sync(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    me = get_json(client, f"{GRAPH}/me", {"$select": "displayName,mail,userPrincipalName"})
    label = me.get("mail") or me.get("userPrincipalName")
    if label:
        store.upsert_account(ID, account_label=str(label)[:120])
    stats = {"messages": 0, "events": 0, "vault_updated": 0, "pruned": 0}
    if payload.get("mail", True) is not False:
        days = _ingest.clamp_int(payload, "days", 3, 1, 30)
        for m in _inbox(client, now - days * 86400, _ingest.clamp_int(payload, "max", MAX_MAIL, 1, MAX_MAIL), full=True):
            p = parse_message(m)
            if not p["id"]:
                continue
            stats["messages"] += 1
            key = re.sub(r"[^A-Za-z0-9_-]", "_", str(p["id"]))[-120:]
            if _ingest.to_vault(source=ID, path=f"microsoft365/mail/{key}", title=str(p["subject"])[:300],
                                content=_mail_doc(p), mtime=p["received"],
                                metadata={"kind": "email", "from": p["from"], "link": p["link"],
                                          "untrusted": True, "tags": ["email", "outlook"]}):
                stats["vault_updated"] += 1
    if payload.get("calendar", True) is not False:
        back = _ingest.clamp_int(payload, "days_back", 7, 0, 90)
        ahead = _ingest.clamp_int(payload, "days_ahead", 30, 1, 365)
        start, end = now - back * 86400, now + ahead * 86400
        keep: set[str] = set()
        for ev in _events(client, start, end):
            st = _event_start(ev)
            if st is None or ev.get("isCancelled") or not ev.get("id"):
                continue
            key = re.sub(r"[^A-Za-z0-9_-]", "_", str(ev["id"]))[-120:]
            uid = f"m365:{key}"
            title = str(ev.get("subject") or "(sin título)")[:300]
            _ingest.upsert_calendar_event(uid=uid, title=title, starts_at=st, all_day=bool(ev.get("isAllDay")), source=ID)
            if _ingest.to_vault(source=ID, path=f"microsoft365/event/{key}", title=title, content=_event_doc(ev, st),
                                mtime=_ts(ev.get("lastModifiedDateTime")),
                                metadata={"kind": "event", "starts_at": st, "link": ev.get("webLink"),
                                          "tags": ["calendar", "outlook"]}):
                stats["vault_updated"] += 1
            keep.add(uid)
            stats["events"] += 1
        stats["pruned"] = _ingest.prune_calendar(ID, "m365:", int(start), int(end), keep)
    return stats


# ---------- writes (pending until confirmed) ----------

def _bad(msg: str) -> actions.ActionError:
    return actions.ActionError("bad_payload", msg, 400)


def graph_send(client: Any, method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    if urlparse(url).hostname != "graph.microsoft.com":
        raise GraphError(400, "host_not_graph")
    r = client.request(method, url, json=body) if body is not None else client.request(method, url)
    if r.status_code >= 400:
        code = ""
        try:
            err = r.json().get("error")
            code = str(err.get("code") if isinstance(err, dict) else err or "")
        except (ValueError, AttributeError):
            pass
        raise GraphError(r.status_code, code[:60])
    if not r.content:
        return {}
    out = r.json()
    return out if isinstance(out, dict) else {}


def _mail_fields(payload: dict[str, Any]) -> dict[str, Any]:
    # same rules as Gmail: plain addresses, To+Cc <= 10, one-line subject, plain-text body
    if "reply_to_id" in payload:
        raise _bad("unsupported fields: reply_to_id")
    return _send_fields(payload)


def _summary_mail(payload: dict[str, Any]) -> str:
    f = _mail_fields(payload)
    cc = f" · cc: {', '.join(f['cc'])}" if f["cc"] else ""
    preview = re.sub(r"\s+", " ", f["body"]).strip()
    preview = preview[:160] + ("…" if len(preview) > 160 else "")
    return (f"Outlook · ENVIAR a {', '.join(f['to'])}{cc} · asunto «{f['subject'] or '(sin asunto)'}»"
            f" · {len(f['body'])} caracteres: {preview}")


def send_mail(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    f = _mail_fields(payload)
    msg = {"subject": f["subject"], "body": {"contentType": "Text", "content": f["body"]},
           "toRecipients": [{"emailAddress": {"address": a}} for a in f["to"]],
           "ccRecipients": [{"emailAddress": {"address": a}} for a in f["cc"]]}
    graph_send(client, "POST", f"{GRAPH}/me/sendMail", {"message": msg, "saveToSentItems": True})
    return {"sent": True, "to": f["to"], "cc": f["cc"]}


_EV_FIELDS = ("title", "start", "end", "location", "description")


def _graph_when(v: Any, key: str) -> tuple[dict[str, str], int, bool]:
    # same parsing as Google Calendar: RFC 3339 with offset, or YYYY-MM-DD
    _g, ts, all_day = _when(v, key)
    dt = datetime.fromtimestamp(ts, tz=UTC)
    return {"dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}, ts, all_day


def _event_id(payload: dict[str, Any]) -> str:
    try:
        return _safe_id(payload.get("event_id"))
    except ValueError as e:
        raise _bad("event_id is invalid") from e


def _event_body(payload: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    allowed = {"event_id", *_EV_FIELDS} if partial else set(_EV_FIELDS)
    extra = sorted(set(payload) - allowed)
    if extra:
        raise _bad(f"unsupported fields: {', '.join(extra)} (no attendees/invitations)")
    body: dict[str, Any] = {}
    title = _text(payload, "title", 255, required=not partial)
    if title is not None:
        body["subject"] = title
    loc = _text(payload, "location", 255)
    if loc is not None:
        body["location"] = {"displayName": loc}
    desc = _text(payload, "description", 5000, oneline=False)
    if desc is not None:
        body["body"] = {"contentType": "Text", "content": desc}
    has_s, has_e = payload.get("start") is not None, payload.get("end") is not None
    if not partial or has_s or has_e:
        if not (has_s and has_e):
            raise _bad("start and end go together")
        s, s_ts, s_day = _graph_when(payload["start"], "start")
        e, e_ts, e_day = _graph_when(payload["end"], "end")
        if s_day != e_day:
            raise _bad("start and end must both be dates or both date-times")
        if e_ts <= s_ts or e_ts - s_ts > 14 * 86400:
            raise _bad("end must be after start (at most 14 days)")
        body.update(start=s, end=e, isAllDay=s_day)
    if partial and not body:
        raise _bad("nothing to change")
    return body


def _when_text(body: dict[str, Any]) -> str:
    if "start" not in body:
        return ""
    if body.get("isAllDay"):
        return f" · {body['start']['dateTime'][:10]} → {body['end']['dateTime'][:10]} (día completo)"
    return f" · {body['start']['dateTime']}Z → {body['end']['dateTime']}Z"


def _summary_create(payload: dict[str, Any]) -> str:
    b = _event_body(payload, partial=False)
    loc = f" · lugar: {b['location']['displayName']}" if b.get("location") else ""
    return f"Outlook Calendar · crear evento «{b['subject']}»{_when_text(b)}{loc} · sin invitaciones"


def _summary_update(payload: dict[str, Any]) -> str:
    b = _event_body(payload, partial=True)
    title = f" → «{b['subject']}»" if "subject" in b else ""
    return (f"Outlook Calendar · cambiar evento {_event_id(payload)[-24:]}{title}{_when_text(b)}"
            " · solo si no tiene asistentes")


def _summary_delete(payload: dict[str, Any]) -> str:
    extra = sorted(set(payload) - {"event_id"})
    if extra:
        raise _bad(f"unsupported fields: {', '.join(extra)}")
    return f"Outlook Calendar · BORRAR evento {_event_id(payload)[-24:]} · solo si no tiene asistentes"


def _no_attendees(client: Any, eid: str) -> None:
    ev = get_json(client, f"{GRAPH}/me/events/{eid}", {"$select": "id,attendees,isOrganizer"})
    if ev.get("attendees"):
        raise actions.ActionError("has_attendees", "Outlook would notify the attendees; change it in Outlook", 409)


def create_event(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    ev = graph_send(client, "POST", f"{GRAPH}/me/events", _event_body(payload, partial=False))
    return {"id": ev.get("id"), "link": ev.get("webLink"),
            "note": "se verá en la agenda y el vault en la próxima sincronización"}


def update_event(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    body = _event_body(payload, partial=True)
    eid = _event_id(payload)
    _no_attendees(client, eid)
    ev = graph_send(client, "PATCH", f"{GRAPH}/me/events/{eid}", body)
    return {"id": ev.get("id") or eid, "link": ev.get("webLink")}


def delete_event(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    _summary_delete(payload)
    eid = _event_id(payload)
    _no_attendees(client, eid)
    graph_send(client, "DELETE", f"{GRAPH}/me/events/{eid}")
    return {"id": eid, "status": "deleted", "note": "desaparecerá de la agenda en la próxima sincronización"}


SPEC = registry.register(registry.ConnectorSpec(
    id=ID, name="Microsoft 365 (Outlook)", auth="oauth_pkce",
    allowed_hosts=("login.microsoftonline.com", "graph.microsoft.com"),
    authorize_url=f"{AUTH}/authorize", token_url=f"{AUTH}/token", revoke_url="",
    read_scopes=READ_SCOPES, write_scopes=WRITE_SCOPES, client_secret_required=False,
    extra_authorize_params={"response_mode": "query", "prompt": "select_account"},
    docs_url="https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow",
    redirect_host="localhost",
    actions={
        "list_messages": registry.ActionSpec("list_messages", "read", "List Inbox messages", list_messages),
        "get_message": registry.ActionSpec("get_message", "read", "Read one message as text", get_message),
        "list_events": registry.ActionSpec("list_events", "read", "List calendar events", list_events),
        "sync": registry.ActionSpec("sync", "read", "Import Inbox mail and calendar into the vault and agenda", sync),
        "send_mail": registry.ActionSpec("send_mail", "write", "Send a plain-text email", send_mail, _summary_mail),
        "create_event": registry.ActionSpec("create_event", "write", "Create an event (no invitations)",
                                            create_event, _summary_create),
        "update_event": registry.ActionSpec("update_event", "write", "Change an event without attendees",
                                            update_event, _summary_update),
        "delete_event": registry.ActionSpec("delete_event", "write", "Delete an event without attendees",
                                            delete_event, _summary_delete),
    },
))
