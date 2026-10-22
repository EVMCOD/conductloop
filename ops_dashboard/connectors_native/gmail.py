"""Gmail connector — read + ingest, and sending behind confirmation.

Scope (read): ``gmail.readonly`` — Google classifies it as **restricted** (see
the guide on /connectors: fine for your own BYOK app in "Testing", which shows
the unverified-app screen and expires every 7 days). There is no narrower
scope that returns message bodies: ``gmail.metadata`` is restricted too.

Actions:
* ``list_messages`` (read) — id, from, subject, date and snippet for a Gmail
  search (``q``), newest first.
* ``get_message`` (read) — one message as plain text.
* ``sync`` (read) — ingest recent mail (default ``newer_than:3d``, max 50,
  never spam/trash) into the vault (``documents``, source ``gmail``) as plain
  text with From/To/Date/Subject. Attachments are not downloaded (only their
  names are listed). Nothing leaves this Mac except the calls to Gmail.

Write (only with permission ``write``, which adds ``gmail.send`` — send only:
no reading beyond ``gmail.readonly``, no drafts, labels or deletes):
* ``send_message`` — plain-text mail to 1–10 plain addresses (To + Cc, no Bcc,
  no display names), subject, body, optionally as a reply in a thread
  (``reply_to_id``: In-Reply-To/References/threadId taken from that message).
  It is queued by ``connector_actions`` and sent only after you confirm the
  exact payload on /connectors (15-minute expiry). The confirmation card shows
  every recipient first.

Mail is untrusted input: whatever an email says, the agent cannot send or
change anything without that confirmation card.
"""
from __future__ import annotations

import base64
import html
import re
from email import policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any

from .. import connector_actions as actions
from .. import connector_registry as registry
from .. import connector_store as store
from . import _google, _ingest

ID = "gmail"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
READ_SCOPES = (_google.scope("gmail.readonly"),)
WRITE_SCOPES = (_google.scope("gmail.send"),)
MAX_RECIPIENTS = 10
MAX_BODY = 20000
_ADDR = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                   r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")
_SEND_FIELDS = {"to", "cc", "subject", "body", "reply_to_id"}
MAX_SYNC = 200
_HEADERS = ("From", "To", "Cc", "Date", "Subject")
_TAG = re.compile(r"<[^>]+>")
_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_BR = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>", re.I)
_WS = re.compile(r"\n{3,}")


def _b64(data: str, charset: str = "utf-8") -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, TypeError):
        return ""
    try:
        return raw.decode(charset, "replace")
    except LookupError:                  # unknown charset name: best effort
        return raw.decode("utf-8", "replace")


def _charset(part: dict[str, Any]) -> str:
    """The part's declared charset (Content-Type), UTF-8 when absent."""
    for h in part.get("headers") or []:
        if isinstance(h, dict) and str(h.get("name", "")).lower() == "content-type":
            m = re.search(r"""charset\s*=\s*["']?([A-Za-z0-9._:-]{1,40})""", str(h.get("value") or ""), re.I)
            if m:
                return m.group(1)
    return "utf-8"


def html_to_text(s: str) -> str:
    s = _STYLE.sub("", s)
    s = _BR.sub("\n", s)
    s = html.unescape(_TAG.sub("", s))
    return _WS.sub("\n\n", "\n".join(line.strip() for line in s.splitlines())).strip()


MAX_EXTERNAL_BODIES = 4


def _walk(part: dict[str, Any], out: dict[str, Any], fetch: Any = None) -> None:
    mime = str(part.get("mimeType") or "")
    body = part.get("body") or {}
    kind = {"text/plain": "plain", "text/html": "html"}.get(mime)
    if part.get("filename"):
        out["attachments"].append(str(part["filename"])[:200])
    elif kind and body.get("data"):
        out[kind].append(_b64(body["data"], _charset(part)))
    elif kind and body.get("attachmentId") and fetch is not None and out["_fetched"] < MAX_EXTERNAL_BODIES:
        # Large text bodies are stored apart (attachmentId, no inline data):
        # fetch them instead of falling back to the snippet.
        out["_fetched"] += 1
        data = fetch(str(body["attachmentId"]))
        if data:
            out[kind].append(_b64(data, _charset(part)))
    for p in part.get("parts") or []:
        if isinstance(p, dict):
            _walk(p, out, fetch)


def parse_message(msg: dict[str, Any], fetch: Any = None) -> dict[str, Any]:
    """``fetch(attachment_id) -> base64url data`` loads text bodies Gmail
    stored apart; without it they are skipped (metadata-only listings)."""
    payload = msg.get("payload") or {}
    headers = {h.get("name"): h.get("value") for h in payload.get("headers") or [] if isinstance(h, dict)}
    parts: dict[str, Any] = {"plain": [], "html": [], "attachments": [], "_fetched": 0}
    _walk(payload, parts, fetch)
    text = "\n\n".join(p.strip() for p in parts["plain"] if p.strip()) or \
        "\n\n".join(html_to_text(h) for h in parts["html"])
    try:
        ts = int(msg.get("internalDate", 0)) // 1000 or None
    except (TypeError, ValueError):
        ts = None
    if ts is None and headers.get("Date"):
        try:
            ts = int(parsedate_to_datetime(str(headers["Date"])).timestamp())
        except (TypeError, ValueError):
            ts = None
    return {"id": msg.get("id"), "thread_id": msg.get("threadId"), "labels": list(msg.get("labelIds") or []),
            "snippet": html.unescape(str(msg.get("snippet") or "")),
            "headers": {k: str(headers[k])[:500] for k in _HEADERS if headers.get(k)},
            "text": text, "attachments": parts["attachments"], "ts": ts}


def _list_ids(client: Any, q: str, limit: int) -> list[str]:
    ids: list[str] = []
    token = None
    while len(ids) < limit:
        params: dict[str, Any] = {"q": q, "maxResults": min(100, limit - len(ids))}
        if token:
            params["pageToken"] = token
        body = _google.get_json(client, f"{API}/messages", params)
        ids += [str(m["id"]) for m in body.get("messages") or [] if isinstance(m, dict) and m.get("id")]
        token = body.get("nextPageToken")
        if not token:
            break
    return ids[:limit]


def _get(client: Any, msg_id: str, fmt: str = "full") -> dict[str, Any]:
    params: dict[str, Any] = {"format": fmt}
    if fmt == "metadata":
        params["metadataHeaders"] = list(_HEADERS)
    return _google.get_json(client, f"{API}/messages/{msg_id}", params)


def _safe_id(v: Any) -> str:
    s = str(v or "")
    if not re.fullmatch(r"[A-Za-z0-9]{1,64}", s):
        raise ValueError("bad message id")
    return s


def list_messages(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    q = str(payload.get("q") or "in:inbox")[:300] + " -in:spam -in:trash"
    limit = _ingest.clamp_int(payload, "max", 20, 1, 50)
    out = []
    for mid in _list_ids(client, q, limit):
        m = parse_message(_get(client, mid, "metadata"))
        out.append({"id": m["id"], "from": m["headers"].get("From"), "subject": m["headers"].get("Subject"),
                    "date": m["ts"], "snippet": m["snippet"][:300]})
    return {"q": q, "messages": out}


def _body_fetcher(client: Any, msg_id: str) -> Any:
    def fetch(att_id: str) -> str | None:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,2048}", att_id):
            return None
        try:
            data = _google.get_json(client, f"{API}/messages/{msg_id}/attachments/{att_id}").get("data")
        except _google.GoogleAPIError:
            return None
        return data if isinstance(data, str) else None
    return fetch


def get_message(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    mid = _safe_id(payload.get("id"))
    m = parse_message(_get(client, mid), _body_fetcher(client, mid))
    return {"id": m["id"], "headers": m["headers"], "text": m["text"][:20000], "attachments": m["attachments"]}


def _doc(m: dict[str, Any]) -> str:
    h = m["headers"]
    lines = [f"# {h.get('Subject') or '(sin asunto)'}", ""]
    lines += [f"- {k}: {h[k]}" for k in ("From", "To", "Cc", "Date") if h.get(k)]
    if m["attachments"]:
        lines.append("- Adjuntos: " + ", ".join(m["attachments"]))
    return "\n".join(lines) + "\n\n" + (m["text"] or m["snippet"])


def sync(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    days = _ingest.clamp_int(payload, "days", 3, 1, 30)
    limit = _ingest.clamp_int(payload, "max", 50, 1, MAX_SYNC)
    q = f"newer_than:{days}d -in:spam -in:trash"
    extra = str(payload.get("q") or "").strip()
    if extra:
        q += " " + extra[:200]
    try:
        prof = _google.get_json(client, f"{API}/profile")
        if isinstance(prof.get("emailAddress"), str):
            store.upsert_account(ID, account_label=prof["emailAddress"][:120])
    except _google.GoogleAPIError:
        pass
    stats = {"messages": 0, "vault_updated": 0}
    for mid in _list_ids(client, q, limit):
        m = parse_message(_get(client, mid), _body_fetcher(client, _safe_id(mid)))
        stats["messages"] += 1
        if _ingest.to_vault(source=ID, path=f"gmail/{m['id']}", title=(m["headers"].get("Subject") or "(sin asunto)")[:300],
                            content=_doc(m), mtime=m["ts"],
                            metadata={"kind": "email", "thread_id": m["thread_id"], "labels": m["labels"],
                                      "from": m["headers"].get("From"), "date": m["ts"], "tags": ["email", "gmail"]}):
            stats["vault_updated"] += 1
    return stats


# ---------- send (pending until confirmed) ----------

def _bad(msg: str) -> actions.ActionError:
    return actions.ActionError("bad_payload", msg, 400)


def _addrs(v: Any, key: str) -> list[str]:
    if v is None or v == "" or v == []:
        return []
    items = [v] if isinstance(v, str) else v
    if not isinstance(items, list) or not all(isinstance(a, str) for a in items):
        raise _bad(f"{key} must be an address or a list of addresses")
    out: list[str] = []
    for a in items:
        a = a.strip()
        if len(a) > 254 or not _ADDR.fullmatch(a):
            raise _bad(f"{key}: {a[:80]!r} is not a plain address (no names, commas or brackets)")
        if a.lower() not in {x.lower() for x in out}:
            out.append(a)
    return out


def _send_fields(payload: dict[str, Any]) -> dict[str, Any]:
    extra = sorted(set(payload) - _SEND_FIELDS)
    if extra:
        raise _bad(f"unsupported fields: {', '.join(extra)} (no bcc, attachments or html)")
    to, cc = _addrs(payload.get("to"), "to"), _addrs(payload.get("cc"), "cc")
    cc = [a for a in cc if a.lower() not in {x.lower() for x in to}]
    if not to:
        raise _bad("to is required")
    if len(to) + len(cc) > MAX_RECIPIENTS:
        raise _bad(f"at most {MAX_RECIPIENTS} recipients")
    subject = payload.get("subject", "")
    if not isinstance(subject, str) or len(subject) > 300 or any(c in subject for c in "\r\n\x00"):
        raise _bad("subject must be one line <= 300 chars")
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY or "\x00" in body:
        raise _bad(f"body must be non-empty text <= {MAX_BODY} chars")
    reply = payload.get("reply_to_id")
    if reply is not None:
        try:
            reply = _safe_id(reply)
        except ValueError as e:
            raise _bad("reply_to_id is not a Gmail message id") from e
    return {"to": to, "cc": cc, "subject": subject.strip(), "body": body, "reply_to_id": reply}


def _summary_send(payload: dict[str, Any]) -> str:
    f = _send_fields(payload)
    cc = f" · cc: {', '.join(f['cc'])}" if f["cc"] else ""
    reply = f" · respuesta al correo {f['reply_to_id']}" if f["reply_to_id"] else ""
    preview = re.sub(r"\s+", " ", f["body"]).strip()
    preview = preview[:160] + ("…" if len(preview) > 160 else "")
    return (f"Gmail · ENVIAR a {', '.join(f['to'])}{cc} · asunto «{f['subject'] or '(sin asunto)'}»{reply}"
            f" · {len(f['body'])} caracteres: {preview}")


def build_raw(f: dict[str, Any], parent: dict[str, Any] | None = None) -> str:
    msg = EmailMessage(policy=policy.SMTP)
    msg["To"] = ", ".join(f["to"])
    if f["cc"]:
        msg["Cc"] = ", ".join(f["cc"])
    subject = f["subject"]
    if parent:
        hdrs = {h.get("name", "").lower(): str(h.get("value", "")) for h in
                (parent.get("payload") or {}).get("headers") or []}
        mid = hdrs.get("message-id", "")
        if mid and re.fullmatch(r"<[^<>\s]{1,250}>", mid):
            msg["In-Reply-To"] = mid
            refs = " ".join(r for r in hdrs.get("references", "").split() if re.fullmatch(r"<[^<>\s]{1,250}>", r))
            msg["References"] = (refs + " " + mid).strip()[-900:]
        if not subject:
            ps = re.sub(r"[\r\n]", " ", hdrs.get("subject", ""))[:290]
            subject = ps if ps.lower().startswith("re:") else f"Re: {ps}"
    msg["Subject"] = subject
    msg.set_content(f["body"])
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")


def send_message(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    f = _send_fields(payload)
    parent = None
    body: dict[str, Any] = {}
    if f["reply_to_id"]:
        parent = _google.get_json(client, f"{API}/messages/{f['reply_to_id']}",
                                  {"format": "metadata", "metadataHeaders": ["Message-ID", "References", "Subject"]})
        if parent.get("threadId"):
            body["threadId"] = str(parent["threadId"])
    body["raw"] = build_raw(f, parent)
    sent = _google.send_json(client, "POST", f"{API}/messages/send", body=body)
    return {"id": sent.get("id"), "thread_id": sent.get("threadId"), "to": f["to"], "cc": f["cc"]}


SPEC = registry.register(registry.ConnectorSpec(
    id=ID, name="Gmail",
    allowed_hosts=(*_google.AUTH_HOSTS, "gmail.googleapis.com"),
    read_scopes=READ_SCOPES,
    write_scopes=WRITE_SCOPES,
    actions={
        "list_messages": registry.ActionSpec("list_messages", "read", "Search your mail (headers + snippet)",
                                             list_messages),
        "get_message": registry.ActionSpec("get_message", "read", "Read one message as text", get_message),
        "sync": registry.ActionSpec("sync", "read", "Import recent mail into the vault", sync),
        "send_message": registry.ActionSpec("send_message", "write", "Send a plain-text email",
                                            send_message, _summary_send),
    },
    **_google.oauth_kwargs(),
))
_google.join_group(ID)
