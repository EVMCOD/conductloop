"""Notion — read + ingest pages into the vault, reusing the existing Notion
token connection (no second copy of the token, no OAuth app needed), and two
additive writes behind confirmation.

Token: ``token_mode="connections"`` with provider ``notion`` — the same
``connections`` row the "Conexiones" view already uses (Fernet-encrypted, key
in the keychain). Pick that connection on /connectors, or paste an internal
integration token (it creates/updates the ``conectores`` row of that same
table). The token only sees the pages you shared with the integration
("Connections" menu of each page in Notion), so that is the real scope; give
the integration only **Read content** for this PR.

Sync: ``POST /v1/search`` (a read; Notion's search is POST-only) for pages,
newest edit first (max 50, or ``max_pages``), then each page's blocks
(``GET /v1/blocks/{id}/children``, max 3 pages of 100, one level of nested
children for toggles/lists) rendered as plain text. Databases rows are pages
too and come along; database schemas and files are not downloaded.

Writes (only after you allow writes for Notion on /connectors, and each one
confirmed with its exact payload; the integration needs **Insert content**):
* ``append_to_page`` — add paragraphs at the end of a page.
* ``create_page`` — create a sub-page (title + paragraphs) under a page.
Additive only: nothing here edits, moves, archives or deletes existing
content, and there are no database/property writes. Plain text only
(no links, mentions or embeds are generated).

``Notion-Version: 2022-06-28``, the same as the Conexiones check
(``connectors.test_notion``).
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from .. import connector_registry as registry
from . import base

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
HEADERS = {"Notion-Version": VERSION}
MAX_PAGES = 50
MAX_BLOCK_PAGES = 3
MAX_NESTED = 30
ID_RE = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
_NESTED = {"toggle", "bulleted_list_item", "numbered_list_item", "to_do", "quote", "callout"}


def _ts(iso: Any) -> int:
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _plain(rich: Any) -> str:
    return "".join(str(r.get("plain_text") or "") for r in rich or [] if isinstance(r, dict))


def page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            t = _plain(prop.get("title"))
            if t:
                return t
    return "(sin título)"


def whoami(c: base.TokenClient) -> str:
    me = c.get_json(f"{API}/users/me", headers=HEADERS)
    bot = me.get("bot") or {}
    return str(bot.get("workspace_name") or me.get("name") or "Notion")


def _block_line(b: dict, depth: int) -> str:
    t = str(b.get("type") or "")
    data = b.get(t) or {}
    text = _plain(data.get("rich_text")) if isinstance(data, dict) else ""
    pad = "  " * depth
    if t.startswith("heading_"):
        return f"{'#' * (int(t[-1]) + 1)} {text}"
    if t == "bulleted_list_item":
        return f"{pad}- {text}"
    if t == "numbered_list_item":
        return f"{pad}1. {text}"
    if t == "to_do":
        return f"{pad}- [{'x' if data.get('checked') else ' '}] {text}"
    if t == "quote":
        return f"{pad}> {text}"
    if t == "code":
        return f"```{data.get('language') or ''}\n{text}\n```"
    if t == "callout":
        return f"{pad}> {text}"
    if t == "toggle":
        return f"{pad}▸ {text}"
    if t == "child_page":
        return f"{pad}[subpágina] {data.get('title') or ''}"
    if t == "child_database":
        return f"{pad}[base de datos] {data.get('title') or ''}"
    if t in ("bookmark", "embed", "link_preview"):
        return f"{pad}{data.get('url') or ''}"
    if t == "divider":
        return "---"
    if t in ("image", "file", "pdf", "video", "audio"):
        return f"{pad}[{t}] {_plain(data.get('caption'))}".rstrip()
    return f"{pad}{text}" if text else ""


def _children(c: base.TokenClient, block_id: str, max_pages: int) -> list[dict]:
    out: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        res = c.get_json(f"{API}/blocks/{block_id}/children", params=params, headers=HEADERS)
        out += [b for b in res.get("results") or [] if isinstance(b, dict)]
        cursor = res.get("next_cursor") if res.get("has_more") else None
        if not cursor:
            break
    return out


def page_text(c: base.TokenClient, page_id: str) -> str:
    lines: list[str] = []
    nested = 0
    for b in _children(c, page_id, MAX_BLOCK_PAGES):
        line = _block_line(b, 0)
        if line:
            lines.append(line)
        if b.get("has_children") and b.get("type") in _NESTED and nested < MAX_NESTED and ID_RE.fullmatch(str(b.get("id"))):
            nested += 1
            for sub in _children(c, str(b["id"]), 1):
                sl = _block_line(sub, 1)
                if sl:
                    lines.append(sl)
    return "\n".join(lines)


def _search_pages(c: base.TokenClient, limit: int) -> list[dict]:
    out: list[dict] = []
    cursor = None
    while len(out) < limit:
        body: dict[str, Any] = {"filter": {"property": "object", "value": "page"},
                                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
                                "page_size": min(100, limit - len(out))}
        if cursor:
            body["start_cursor"] = cursor
        res = c.post_json(f"{API}/search", body, headers=HEADERS)
        out += [p for p in res.get("results") or [] if isinstance(p, dict) and not p.get("archived")
                and not p.get("in_trash")]
        cursor = res.get("next_cursor") if res.get("has_more") else None
        if not cursor:
            break
    return out[:limit]


def sync(c: base.TokenClient, config: dict) -> Iterator[base.Item]:
    try:
        limit = max(1, min(MAX_PAGES, int(config.get("max_pages") or MAX_PAGES)))
    except ValueError:
        limit = MAX_PAGES
    for p in _search_pages(c, limit):
        pid = str(p.get("id") or "")
        if not ID_RE.fullmatch(pid):
            continue
        title = page_title(p)
        yield base.Item(kind="page", key=pid, title=title, content=f"# {title}\n\n{page_text(c, pid)}",
                        url=str(p.get("url") or ""), updated=_ts(p.get("last_edited_time")), tags=("notion",))


def _page_id(payload: dict) -> str:
    pid = str(payload.get("page_id") or "")
    if not ID_RE.fullmatch(pid):
        raise base.TokenError("bad_page_id", "page_id must be a Notion page id")
    return pid


def search(c: base.TokenClient, payload: dict) -> dict:
    q = payload.get("query", "")
    if not isinstance(q, str) or len(q) > 200:
        raise base.TokenError("bad_query", "query must be text (<= 200 chars)")
    res = c.post_json(f"{API}/search", {"query": q, "filter": {"property": "object", "value": "page"},
                                        "page_size": 20}, headers=HEADERS)
    return {"pages": [{"id": p.get("id"), "title": base.clean_text(page_title(p), 300), "url": p.get("url"),
                       "last_edited": _ts(p.get("last_edited_time"))} for p in res.get("results") or []]}


def get_page(c: base.TokenClient, payload: dict) -> dict:
    pid = _page_id(payload)
    p = c.get_json(f"{API}/pages/{pid}", headers=HEADERS)
    return {"id": pid, "title": base.clean_text(page_title(p), 300), "url": p.get("url"),
            "text": base.clean_text(page_text(c, pid), 50000)}


# ---------- additive writes (pending until confirmed) ----------

MAX_WRITE = 20000
MAX_BLOCK_TEXT = 2000           # Notion's rich_text content limit
MAX_BLOCKS = 100                # Notion's children-per-request limit


def _bad(msg: str) -> base.TokenError:
    return base.TokenError("bad_payload", msg, 400)


def _paragraphs(text: Any) -> list[dict]:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_WRITE or "\x00" in text:
        raise _bad(f"text must be non-empty, <= {MAX_WRITE} chars")
    blocks: list[dict] = []
    for para in re.split(r"\n\s*\n", base.clean_text(text, MAX_WRITE).strip()):
        for i in range(0, len(para), MAX_BLOCK_TEXT):
            chunk = para[i:i + MAX_BLOCK_TEXT]
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]}})
    if len(blocks) > MAX_BLOCKS:
        raise _bad(f"at most {MAX_BLOCKS} paragraphs")
    return blocks


def _only(payload: dict, allowed: set[str]) -> None:
    extra = sorted(set(payload) - allowed)
    if extra:
        raise _bad(f"unsupported fields: {', '.join(extra)}")


def _title(payload: dict) -> str:
    t = payload.get("title")
    if not isinstance(t, str) or not t.strip() or len(t) > 200 or any(c in t for c in "\r\n\x00"):
        raise _bad("title must be one line, 1-200 chars")
    return t.strip()


def _preview(text: str) -> str:
    p = re.sub(r"\s+", " ", base.clean_text(text, MAX_WRITE)).strip()
    return p[:160] + ("…" if len(p) > 160 else "")


def _sum_append(p: dict) -> str:
    _only(p, {"page_id", "text"})
    pid, blocks = _page_id(p), _paragraphs(p.get("text"))
    return f"Notion · AÑADIR al final de la página {pid} · {len(blocks)} párrafo(s): {_preview(p['text'])}"


def _sum_create(p: dict) -> str:
    _only(p, {"parent_page_id", "title", "text"})
    parent = _page_id({"page_id": p.get("parent_page_id")})
    title = _title(p)
    n = len(_paragraphs(p["text"])) if p.get("text") else 0
    body = f" · {n} párrafo(s): {_preview(p['text'])}" if n else ""
    return f"Notion · CREAR página «{title}» dentro de {parent}{body}"


def append_to_page(c: base.TokenClient, payload: dict) -> dict:
    _sum_append(payload)
    pid = _page_id(payload)
    res = c._check(c.request("PATCH", f"{API}/blocks/{pid}/children", headers=HEADERS,
                             json={"children": _paragraphs(payload["text"])})).json()
    return {"page_id": pid, "added": len(res.get("results") or [])}


def create_page(c: base.TokenClient, payload: dict) -> dict:
    _sum_create(payload)
    body: dict[str, Any] = {"parent": {"page_id": _page_id({"page_id": payload["parent_page_id"]})},
                            "properties": {"title": {"title": [{"type": "text", "text": {"content": _title(payload)}}]}}}
    if payload.get("text"):
        body["children"] = _paragraphs(payload["text"])
    res = c.post_json(f"{API}/pages", body, headers=HEADERS)
    return {"id": res.get("id"), "url": res.get("url")}


CONNECTOR = base.register_connector(base.TokenConnector(
    spec=registry.ConnectorSpec(
        id="notion", name="Notion", auth="token", allowed_hosts=("api.notion.com",),
        docs_url="https://developers.notion.com/docs/authorization",
        actions={
            "sync": registry.ActionSpec("sync", "read", "Import shared pages into the vault", base.sync_action("notion")),
            "search": registry.ActionSpec("search", "read", "Search pages shared with the integration",
                                          base.token_action("notion", search)),
            "get_page": registry.ActionSpec("get_page", "read", "Read one page as text",
                                            base.token_action("notion", get_page)),
            "append_to_page": registry.ActionSpec("append_to_page", "write", "Add paragraphs at the end of a page",
                                                  base.token_action("notion", append_to_page), summarize=_sum_append),
            "create_page": registry.ActionSpec("create_page", "write", "Create a sub-page under a page",
                                               base.token_action("notion", create_page), summarize=_sum_create),
        }),
    token_mode="connections", connections_provider="notion", connections_key="token",
    whoami=whoami, sync=sync,
    config_fields=(base.ConfigField("max_pages", "text", "Máximo de páginas por sincronización (1-50)",
                                    "Max pages per sync (1-50)", pattern=r"[1-9][0-9]?"),),
    token_help_es="Usa tu conexión de Notion de «Conexiones» (no se duplica el token) o pega el token de una integración interna con «Read content» («Insert content» solo si vas a escribir). Solo verá las páginas que compartas con la integración (menú «Conexiones» de cada página).",
    token_help_en="Use your Notion connection from \u201cConnections\u201d (the token is not duplicated) or paste an internal integration token with \u201cRead content\u201d (\u201cInsert content\u201d only if you will write). It only sees the pages you share with the integration (each page\u2019s \u201cConnections\u201d menu).",
))
