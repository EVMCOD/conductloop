"""Linear (personal API key) — read + ingest into the vault; one write
(comment on an issue) that always waits for confirmation.

Reuses the existing Linear token connection (``connections`` table, provider
``linear``, key ``api_key``) so the key is never stored twice. Personal API
keys go in ``Authorization: <key>`` (no scheme). Linear lets you create a
read-only key; *write* is only needed for comments.

Sync: the 50 most recently updated issues of the configured team keys, or,
with no teams configured, the issues assigned to you.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from .. import connector_registry as registry
from . import base

API = "https://api.linear.app/graphql"
TEAM_RE = r"[A-Z][A-Z0-9_]{0,9}"
UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
FIRST = 50

_ISSUE_FIELDS = ("id identifier title description url updatedAt priorityLabel "
                 "state { name } team { key } assignee { name } labels { nodes { name } }")
Q_VIEWER = "query Viewer { viewer { id name email } }"
Q_ASSIGNED = ("query AssignedIssues($first: Int!) { viewer { assignedIssues(first: $first, orderBy: updatedAt) "
              f"{{ nodes {{ {_ISSUE_FIELDS} }} }} }} }}")
Q_TEAMS = ("query TeamIssues($first: Int!, $teams: [String!]) { issues(first: $first, orderBy: updatedAt, "
           f"filter: {{ team: {{ key: {{ in: $teams }} }} }}) {{ nodes {{ {_ISSUE_FIELDS} }} }} }}")
Q_SEARCH = ("query SearchIssues($term: String!) { searchIssues(term: $term, first: 20) "
            "{ nodes { id identifier title url state { name } } } }")
M_COMMENT = ("mutation CommentCreate($issueId: String!, $body: String!) { commentCreate(input: "
             "{ issueId: $issueId, body: $body }) { success comment { id url } } }")


def _gql(c: base.TokenClient, op: str, query: str, variables: dict[str, Any] | None = None) -> dict:
    r = c.request("POST", API, json={"operationName": op, "query": query, "variables": variables or {}})
    try:
        res = r.json()
    except ValueError:
        res = None
    if r.status_code >= 400 and not (isinstance(res, dict) and res.get("errors")):
        res = c._check(r).json()                 # maps 401/403/429/5xx to TokenError
    if not isinstance(res, dict):
        raise base.TokenError("bad_response", "linear: response is not JSON", 502)
    errs = res.get("errors")
    if errs:
        ext = (errs[0].get("extensions") or {}) if isinstance(errs[0], dict) else {}
        kind = str(ext.get("type") or ext.get("code") or "").lower()
        if str(ext.get("code") or "").upper() == "RATELIMITED":   # HTTP 400 + this code
            raise base.TokenError("rate_limited", "linear: rate limited", 429)
        if "auth" in kind:
            raise base.TokenError("unauthorized", "linear: authentication error", 401)
        raise base.TokenError("graphql_error", base.clean_text((errs[0] or {}).get("message"), 200), 502)
    return res.get("data") or {}


def _ts(iso: Any) -> int:
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def whoami(c: base.TokenClient) -> str:
    v = _gql(c, "Viewer", Q_VIEWER).get("viewer") or {}
    return str(v.get("name") or v.get("email") or "")


def _item(n: dict) -> base.Item:
    labels = [str(x.get("name", "")) for x in ((n.get("labels") or {}).get("nodes") or [])]
    team = (n.get("team") or {}).get("key") or ""
    head = " · ".join(x for x in (n.get("identifier"), (n.get("state") or {}).get("name"), n.get("priorityLabel"),
                                  "@" + ((n.get("assignee") or {}).get("name") or "—")) if x)
    if labels:
        head += " · " + ", ".join(labels)
    return base.Item(kind="issue", key=str(n.get("identifier") or n.get("id")),
                     title=f"{n.get('identifier')} {n.get('title') or ''}",
                     content=f"{head}\nid: {n.get('id')}\n\n{n.get('description') or ''}",
                     url=str(n.get("url") or ""), updated=_ts(n.get("updatedAt")),
                     tags=tuple(["linear", team, *labels][:10]))


def sync(c: base.TokenClient, config: dict) -> Iterator[base.Item]:
    teams = config.get("teams") or []
    if teams:
        nodes = (_gql(c, "TeamIssues", Q_TEAMS, {"first": FIRST, "teams": teams}).get("issues") or {}).get("nodes")
    else:
        viewer = _gql(c, "AssignedIssues", Q_ASSIGNED, {"first": FIRST}).get("viewer") or {}
        nodes = (viewer.get("assignedIssues") or {}).get("nodes")
    for n in nodes or []:
        yield _item(n)


def search_issues(c: base.TokenClient, payload: dict) -> dict:
    term = str(payload.get("term") or "").strip()
    if not term or len(term) > 256:
        raise base.TokenError("bad_query", "term is required (<= 256 chars)")
    nodes = (_gql(c, "SearchIssues", Q_SEARCH, {"term": term}).get("searchIssues") or {}).get("nodes") or []
    return {"untrusted": True, "items": [
        {"id": n.get("id"), "identifier": n.get("identifier"), "title": base.clean_text(n.get("title"), 300),
         "state": (n.get("state") or {}).get("name"), "url": n.get("url")} for n in nodes]}


def _comment_payload(payload: dict) -> tuple[str, str]:
    issue_id, body = payload.get("issue_id"), payload.get("body")
    if not isinstance(issue_id, str) or not re.fullmatch(UUID_RE, issue_id):
        raise base.TokenError("bad_issue_id", "issue_id must be the issue UUID (see the ingested 'id:' line)")
    if not isinstance(body, str) or not body.strip() or len(body) > 10_000:
        raise base.TokenError("bad_body", "body is required (<= 10000 chars)")
    return issue_id, body


def comment_issue(c: base.TokenClient, payload: dict) -> dict:
    issue_id, body = _comment_payload(payload)
    res = _gql(c, "CommentCreate", M_COMMENT, {"issueId": issue_id, "body": body}).get("commentCreate") or {}
    if not res.get("success"):
        raise base.TokenError("write_failed", "linear: commentCreate returned success=false", 502)
    return {"id": (res.get("comment") or {}).get("id"), "url": (res.get("comment") or {}).get("url")}


def _summary(p: dict) -> str:
    issue_id, _ = _comment_payload(p)        # reject a bad payload before it is queued
    # Only issue_id is sent to Linear. The caller's "identifier" is never
    # checked against it, so it is shown as a claim next to the real target.
    claim = f" (el agente dice que es {str(p['identifier'])[:40]})" if p.get("identifier") else ""
    return f"Linear: comentar en el issue {issue_id}{claim}: «{str(p.get('body', ''))[:200]}»"


CONNECTOR = base.register_connector(base.TokenConnector(
    spec=registry.ConnectorSpec(
        id="linear", name="Linear", auth="token", allowed_hosts=("api.linear.app",),
        docs_url="https://linear.app/settings/account/security",
        actions={
            "sync": registry.ActionSpec("sync", "read", "Import issues into the vault", base.sync_action("linear")),
            "search_issues": registry.ActionSpec("search_issues", "read", "Search issues",
                                                 base.token_action("linear", search_issues)),
            "comment_issue": registry.ActionSpec("comment_issue", "write", "Comment on an issue",
                                                 base.token_action("linear", comment_issue), summarize=_summary),
        }),
    token_mode="connections", connections_provider="linear", connections_key="api_key", auth_scheme="",
    whoami=whoami, sync=sync,
    config_fields=(base.ConfigField("teams", "list", "Equipos (clave, p. ej. ENG; vacío = lo asignado a ti)",
                                    "Teams (key, e.g. ENG; empty = assigned to you)", pattern=TEAM_RE, max_items=20),),
    token_help_es="Usa la conexión de Linear de «Conexiones» o pega una API key personal (Ajustes → Seguridad y acceso); de solo lectura salvo que quieras comentar.",
    token_help_en="Use your Linear connection from \u201cConnections\u201d or paste a personal API key (Settings → Security & access); read-only unless you want to comment.",
))
