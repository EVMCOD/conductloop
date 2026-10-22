"""Slack connector — read + ingest, and posting behind confirmation.

User-token-only OAuth with PKCE (no bot, no client secret), exactly as the
guide on /connectors sets up the app:

* authorize ``https://slack.com/oauth/v2_user/authorize`` and exchange at
  ``oauth.v2.user.access`` (the user token comes back at the top level);
* comma-separated scopes; read = ``channels:read channels:history users:read``;
* redirect ``http://localhost:47823/callback`` — Slack compares the whole
  redirect, port included, so this connector always uses that fixed port
  (``redirect_port``; the receiver binds 127.0.0.1).

Actions:
* ``list_channels`` (read) — public channels (name, member?, topic).
* ``read_channel`` (read) — recent messages of one channel as text.
* ``sync`` (read) — public channels you are a member of (max 20, or the
  ``channel_ids`` you pass) → one vault document per channel and day
  (source ``slack``), last N days (default 3, max 30). User ids become names
  (``users:read``); thread replies are not fetched. Messages are untrusted
  text: nothing in them can post anything.

Write (only with permission ``write``, which adds the user scope
``chat:write``): ``post_message`` posts **as you** to one public channel (or a
thread reply) after you confirm the exact text on /connectors (15-minute
expiry). Plain text up to 4000 chars; mass mentions (``<!here>``,
``<!channel>``, ``<!everyone>``, ``@here``/``@channel``) and user-group pings
are refused; link/media unfurls are off.

Revocation: ``revoke_url`` is empty — disconnecting deletes the token from the
keychain; remove the app from Slack (Your apps / workspace settings) to revoke
it there. ⚠️ ``auth.revoke`` with the framework's form call is unverified.
"""
from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

from .. import connector_actions as actions
from .. import connector_registry as registry
from .. import connector_store as store
from . import _ingest

ID = "slack"
PORT = 47823
API = "https://slack.com/api"
READ_SCOPES = ("channels:read", "channels:history", "users:read")
WRITE_SCOPES = ("chat:write",)
MAX_TEXT = 4000
_MASS = re.compile(r"<!(here|channel|everyone)\b|<!subteam\^|(?<![\w<])@(here|channel|everyone)\b", re.I)
_TS_RE = re.compile(r"\d{9,11}\.\d{6}")
MAX_CHANNELS = 20
MAX_MESSAGES = 200
_SKIP_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name"}
_CHAN_RE = re.compile(r"[CG][A-Z0-9]{2,30}")


class SlackAPIError(RuntimeError):
    def __init__(self, method: str, error: str, status: int = 200) -> None:
        super().__init__(f"slack_{method}:{error}")
        self.method, self.error, self.status = method, error, status


def call(client: Any, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    r = client.get(f"{API}/{method}", params=params or {})
    if r.status_code == 429:
        raise SlackAPIError(method, "ratelimited", 429)
    if r.status_code >= 400:
        raise SlackAPIError(method, f"http_{r.status_code}", r.status_code)
    try:
        body = r.json()
    except ValueError as e:
        raise SlackAPIError(method, "not_json") from e
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise SlackAPIError(method, str(body.get("error") if isinstance(body, dict) else "bad_body")[:60])
    return body


def _paged(client: Any, method: str, params: dict[str, Any], key: str, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(10):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        body = call(client, method, p)
        out += [x for x in body.get(key) or [] if isinstance(x, dict)]
        cursor = str((body.get("response_metadata") or {}).get("next_cursor") or "")
        if not cursor or len(out) >= limit:
            break
    return out[:limit]


def _channel_id(v: Any) -> str:
    s = str(v or "")
    if not _CHAN_RE.fullmatch(s):
        raise ValueError("bad channel id")
    return s


def _channels(client: Any) -> list[dict[str, Any]]:
    items = _paged(client, "conversations.list",
                   {"types": "public_channel", "exclude_archived": "true", "limit": 200}, "channels", 1000)
    return [{"id": c.get("id"), "name": c.get("name"), "is_member": bool(c.get("is_member")),
             "topic": (c.get("topic") or {}).get("value") or ""} for c in items if c.get("id")]


class _Names:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.cache: dict[str, str] = {}
        self.lookups = 0

    def __call__(self, uid: str) -> str:
        if uid not in self.cache:
            name = uid
            if self.lookups < 100 and re.fullmatch(r"[UW][A-Z0-9]{2,30}", uid):
                self.lookups += 1
                try:
                    u = call(self.client, "users.info", {"user": uid}).get("user") or {}
                    prof = u.get("profile") or {}
                    name = prof.get("display_name") or prof.get("real_name") or u.get("name") or uid
                except SlackAPIError:
                    pass
            self.cache[uid] = str(name)[:80]
        return self.cache[uid]


def render(text: str, names: _Names) -> str:
    """Slack mrkdwn → plain text: <@U1> → @Name, <#C1|general> → #general,
    <https://x|label> → label (https://x), entities unescaped."""
    def repl(m: re.Match[str]) -> str:
        inner = m.group(1)
        target, _, label = inner.partition("|")
        if target.startswith("@"):
            return "@" + names(target[1:])
        if target.startswith("#"):
            return "#" + (label or target[1:])
        if target.startswith("!"):
            return "@" + (label or target[1:].split("^")[0])
        return f"{label} ({target})" if label and label != target else target
    out = re.sub(r"<([^<>]{1,500})>", repl, text or "")
    return out.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _history(client: Any, channel: str, oldest: float, limit: int) -> list[dict[str, Any]]:
    msgs = _paged(client, "conversations.history",
                  {"channel": channel, "oldest": f"{oldest:.6f}", "limit": min(200, limit)}, "messages", limit)
    return [m for m in msgs if m.get("type") == "message" and m.get("subtype") not in _SKIP_SUBTYPES
            and (m.get("text") or "").strip()]


def _line(m: dict[str, Any], names: _Names) -> tuple[str, str]:
    ts = float(m.get("ts") or 0)
    dt = datetime.fromtimestamp(ts, tz=UTC)
    who = names(str(m["user"])) if m.get("user") else str(m.get("username") or m.get("bot_id") or "bot")
    replies = f" [{m['reply_count']} respuestas]" if m.get("reply_count") else ""
    return dt.strftime("%Y-%m-%d"), f"{dt.strftime('%H:%M')} UTC — {who}: {render(str(m.get('text')), names)}{replies}"


def list_channels(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"channels": _channels(client)}


def read_channel(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    ch = _channel_id(payload.get("channel"))
    days = _ingest.clamp_int(payload, "days", 1, 1, 30)
    names = _Names(client)
    msgs = _history(client, ch, time.time() - days * 86400, _ingest.clamp_int(payload, "max", 50, 1, MAX_MESSAGES))
    return {"channel": ch, "messages": [_line(m, names)[1] for m in reversed(msgs)]}


def sync(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    days = _ingest.clamp_int(payload, "days", 3, 1, 30)
    me = call(client, "auth.test")
    team = re.sub(r"[^A-Za-z0-9_-]", "_", str(me.get("team_id") or "team"))[:40]
    if me.get("team") or me.get("user"):
        store.upsert_account(ID, account_label=f"{me.get('user') or ''} @ {me.get('team') or ''}".strip()[:120])
    chans = [c for c in _channels(client) if c["is_member"]]
    wanted = payload.get("channel_ids")
    if isinstance(wanted, list) and wanted:
        ids = {_channel_id(w) for w in wanted}
        chans = [c for c in chans if c["id"] in ids]
    chans = chans[:MAX_CHANNELS]
    names = _Names(client)
    stats = {"channels": len(chans), "messages": 0, "vault_updated": 0}
    oldest = time.time() - days * 86400
    for c in chans:
        by_day: dict[str, list[str]] = {}
        latest: dict[str, int] = {}
        for m in reversed(_history(client, c["id"], oldest, MAX_MESSAGES)):
            day, line = _line(m, names)
            by_day.setdefault(day, []).append(line)
            latest[day] = max(latest.get(day, 0), int(float(m.get("ts") or 0)))
            stats["messages"] += 1
        for day, lines in by_day.items():
            title = f"#{c['name']} · {day}"
            doc = f"# Slack {title}\n\n" + "\n".join(lines)
            if _ingest.to_vault(source=ID, path=f"slack/{team}/{c['id']}/{day}", title=title, content=doc,
                                mtime=latest[day] + len(lines),
                                metadata={"kind": "channel_day", "channel": c["name"], "day": day,
                                          "untrusted": True, "tags": ["slack", c["name"]]}):
                stats["vault_updated"] += 1
    return stats


# ---------- post (pending until confirmed) ----------

def _bad(msg: str) -> actions.ActionError:
    return actions.ActionError("bad_payload", msg, 400)


def _post_fields(payload: dict[str, Any]) -> dict[str, Any]:
    extra = sorted(set(payload) - {"channel", "text", "thread_ts"})
    if extra:
        raise _bad(f"unsupported fields: {', '.join(extra)} (no blocks, attachments or as_user)")
    try:
        ch = _channel_id(payload.get("channel"))
    except ValueError as e:
        raise _bad("channel must be a channel id (C…)") from e
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or "\x00" in text:
        raise _bad(f"text must be non-empty, <= {MAX_TEXT} chars")
    if _MASS.search(text):
        raise _bad("mass mentions (@here, @channel, @everyone, user groups) are not allowed")
    ts = payload.get("thread_ts")
    if ts is not None and (not isinstance(ts, str) or not _TS_RE.fullmatch(ts)):
        raise _bad("thread_ts must look like 1792000200.000200")
    return {"channel": ch, "text": text, "thread_ts": ts}


def _summary_post(payload: dict[str, Any]) -> str:
    f = _post_fields(payload)
    where = f"hilo {f['thread_ts']} de {f['channel']}" if f["thread_ts"] else f"canal {f['channel']}"
    preview = re.sub(r"\s+", " ", f["text"]).strip()
    preview = preview[:200] + ("…" if len(preview) > 200 else "")
    return f"Slack · PUBLICAR como tú en {where} · {len(f['text'])} caracteres: {preview}"


def post_message(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    f = _post_fields(payload)
    body: dict[str, Any] = {"channel": f["channel"], "text": f["text"], "unfurl_links": False,
                            "unfurl_media": False, "link_names": False}
    if f["thread_ts"]:
        body["thread_ts"] = f["thread_ts"]
    r = client.post(f"{API}/chat.postMessage", json=body,
                    headers={"Content-Type": "application/json; charset=utf-8"})
    if r.status_code == 429:
        raise SlackAPIError("chat.postMessage", "ratelimited", 429)
    try:
        res = r.json()
    except ValueError as e:
        raise SlackAPIError("chat.postMessage", "not_json", r.status_code) from e
    if r.status_code >= 400 or not isinstance(res, dict) or res.get("ok") is not True:
        raise SlackAPIError("chat.postMessage", str(res.get("error") if isinstance(res, dict) else "bad_body")[:60])
    return {"channel": res.get("channel"), "ts": res.get("ts")}


SPEC = registry.register(registry.ConnectorSpec(
    id=ID, name="Slack", auth="oauth_pkce",
    allowed_hosts=("slack.com",),
    authorize_url="https://slack.com/oauth/v2_user/authorize",
    token_url=f"{API}/oauth.v2.user.access",
    revoke_url="",
    read_scopes=READ_SCOPES,
    write_scopes=WRITE_SCOPES,
    scope_separator=",",
    client_secret_required=False,
    docs_url="https://docs.slack.dev/authentication/using-pkce",
    redirect_host="localhost",
    redirect_port=PORT,
    actions={
        "list_channels": registry.ActionSpec("list_channels", "read", "List public channels", list_channels),
        "read_channel": registry.ActionSpec("read_channel", "read", "Read recent messages of a channel",
                                            read_channel),
        "sync": registry.ActionSpec("sync", "read", "Import your channels' recent messages into the vault", sync),
        "post_message": registry.ActionSpec("post_message", "write", "Post a message as you",
                                            post_message, _summary_post),
    },
))
