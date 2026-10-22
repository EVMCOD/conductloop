"""GitHub (personal access token) — read + ingest into the vault; one write
(comment on an issue/PR) that always waits for confirmation.

Reuses the existing GitHub token connection (``connections`` table, provider
``github``, key ``token``): the Conectores card lets the user pick it, so the
token is never stored twice. A fine-grained PAT with read-only *Issues* and
*Pull requests* (plus *Metadata*) is enough for the sync; *Issues: write* is
only needed for comments.

Sync: for every configured ``owner/repo`` the 50 most recently updated issues
and PRs; with no repos configured, the open issues/PRs assigned to the user.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from urllib.parse import quote

from .. import connector_registry as registry
from . import base

API = "https://api.github.com"
REPO_RE = r"[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,100}"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
PER_PAGE = 50


def _ts(iso: Any) -> int:
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def whoami(c: base.TokenClient) -> str:
    me = c.get_json(f"{API}/user", headers=HEADERS)
    return str(me.get("login") or "")


def _item(it: dict, repo: str) -> base.Item:
    is_pr = "pull_request" in it
    labels = [str(lb.get("name", "")) for lb in it.get("labels") or [] if isinstance(lb, dict)]
    user = (it.get("user") or {}).get("login") or ""
    head = f"{repo}#{it.get('number')} · {it.get('state')} · @{user}"
    if labels:
        head += " · " + ", ".join(labels)
    return base.Item(kind="pr" if is_pr else "issue", key=f"{repo}#{it.get('number')}",
                     title=f"{repo}#{it.get('number')} {it.get('title') or ''}",
                     content=head + "\n\n" + (it.get("body") or ""),
                     url=str(it.get("html_url") or ""), updated=_ts(it.get("updated_at")),
                     tags=tuple(["github", repo, *labels][:10]))


def _repo_of(it: dict) -> str:
    m = re.search(r"/repos/([^/]+/[^/]+)$", str(it.get("repository_url") or ""))
    return m.group(1) if m else "?/?"


def sync(c: base.TokenClient, config: dict) -> Iterator[base.Item]:
    repos = config.get("repos") or []
    if not repos:
        for it in c.get_json(f"{API}/issues", headers=HEADERS,
                             params={"filter": "assigned", "state": "open", "per_page": PER_PAGE}):
            yield _item(it, _repo_of(it))
        return
    # base.ingest stops at MAX_ITEMS: share it out so every configured repo gets
    # its most recently updated items (30 repos × 16 = 480), not just the first 10.
    per_repo = max(1, min(PER_PAGE, base.MAX_ITEMS // len(repos)))
    for repo in repos:
        for it in c.get_json(f"{API}/repos/{repo}/issues", headers=HEADERS,
                             params={"state": "all", "sort": "updated", "direction": "desc", "per_page": per_repo}):
            yield _item(it, repo)


def search_issues(c: base.TokenClient, payload: dict) -> dict:
    q = str(payload.get("q") or "").strip()
    if not q or len(q) > 256:
        raise base.TokenError("bad_query", "q is required (<= 256 chars)")
    res = c.get_json(f"{API}/search/issues", headers=HEADERS, params={"q": q, "per_page": 20})
    return {"untrusted": True, "items": [
        {"repo": _repo_of(it), "number": it.get("number"), "title": base.clean_text(it.get("title"), 300),
         "state": it.get("state"), "url": it.get("html_url"), "is_pr": "pull_request" in it}
        for it in res.get("items") or []]}


def _comment_payload(payload: dict) -> tuple[str, int, str]:
    repo, number, body = payload.get("repo"), payload.get("number"), payload.get("body")
    if not isinstance(repo, str) or not re.fullmatch(REPO_RE, repo):
        raise base.TokenError("bad_repo", "repo must be owner/name")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise base.TokenError("bad_number", "number must be a positive integer")
    if not isinstance(body, str) or not body.strip() or len(body) > 10_000:
        raise base.TokenError("bad_body", "body is required (<= 10000 chars)")
    return repo, number, body


def comment_issue(c: base.TokenClient, payload: dict) -> dict:
    repo, number, body = _comment_payload(payload)
    owner, name = repo.split("/", 1)
    res = c.post_json(f"{API}/repos/{quote(owner)}/{quote(name)}/issues/{number}/comments", {"body": body},
                      headers=HEADERS)
    return {"id": res.get("id"), "url": res.get("html_url")}


def _summary(p: dict) -> str:
    _comment_payload(p)                      # reject a bad payload before it is queued
    return f"GitHub: comentar en {p.get('repo')}#{p.get('number')}: «{str(p.get('body', ''))[:200]}»"


CONNECTOR = base.register_connector(base.TokenConnector(
    spec=registry.ConnectorSpec(
        id="github", name="GitHub", auth="token", allowed_hosts=("api.github.com",),
        docs_url="https://github.com/settings/personal-access-tokens",
        actions={
            "sync": registry.ActionSpec("sync", "read", "Import issues and PRs into the vault", base.sync_action("github")),
            "search_issues": registry.ActionSpec("search_issues", "read", "Search issues and PRs",
                                                 base.token_action("github", search_issues)),
            "comment_issue": registry.ActionSpec("comment_issue", "write", "Comment on an issue or PR",
                                                 base.token_action("github", comment_issue), summarize=_summary),
        }),
    token_mode="connections", connections_provider="github", connections_key="token",
    whoami=whoami, sync=sync,
    config_fields=(base.ConfigField("repos", "list", "Repositorios (owner/repo, uno por línea; vacío = lo asignado a ti)",
                                    "Repositories (owner/repo, one per line; empty = assigned to you)",
                                    pattern=REPO_RE, max_items=30),),
    token_help_es="Usa la conexión de GitHub de «Conexiones» o pega un token fine-grained con Issues y Pull requests en solo lectura (Issues: escritura solo si quieres comentar).",
    token_help_en="Use your GitHub connection from \u201cConnections\u201d or paste a fine-grained token with read-only Issues and Pull requests (Issues: write only to comment).",
))
