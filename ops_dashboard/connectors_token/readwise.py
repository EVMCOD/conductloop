"""Readwise (access token) — read + ingest your books/articles and their
highlights into the vault; one write (save a highlight) that always waits
for confirmation.

The token lives only in the OS keychain and goes in ``Authorization: Token
<token>``. Readwise tokens can't be limited to read-only: the local
permission stays ``read`` until the user grants writes, and each write is
confirmed one by one.

Sync: the export API (``/api/v2/export/``), one vault document per book with
its highlights (discarded ones skipped), at most 5 pages per sync.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from .. import connector_registry as registry
from . import base

API = "https://readwise.io/api/v2"
MAX_PAGES = 5


def _ts(iso: Any) -> int:
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def whoami(c: base.TokenClient) -> str:
    r = c._check(c.request("GET", f"{API}/auth/"))
    if r.status_code != 204:
        raise base.TokenError("unauthorized", "readwise: token not accepted", 401)
    return "Readwise"


def _book(b: dict) -> base.Item | None:
    hl = [h for h in b.get("highlights") or [] if isinstance(h, dict) and not h.get("is_discard")]
    if not hl:
        return None
    title = str(b.get("readable_title") or b.get("title") or "")
    parts = [f"{title} — {b.get('author') or ''} · {b.get('category') or ''} · {b.get('source') or ''}".strip(" —·")]
    if b.get("document_note"):
        parts.append(f"Nota: {b['document_note']}")
    for h in hl:
        line = f"> {h.get('text') or ''}"
        if h.get("note"):
            line += f"\n  Nota: {h['note']}"
        parts.append(line)
    tags = [str(t.get("name", "")) for t in b.get("book_tags") or [] if isinstance(t, dict)]
    updated = max([_ts(h.get("updated_at") or h.get("highlighted_at")) for h in hl] or [0])
    url = str(b.get("readwise_url") or "")
    return base.Item(kind="book", key=str(b.get("user_book_id")), title=title, content="\n\n".join(parts),
                     url=url, updated=updated, tags=tuple(["readwise", str(b.get("category") or ""), *tags][:10]))


def sync(c: base.TokenClient, config: dict) -> Iterator[base.Item]:
    cursor = None
    for _ in range(MAX_PAGES):
        params = {"pageCursor": cursor} if cursor else None
        res = c.get_json(f"{API}/export/", params=params)
        for b in res.get("results") or []:
            it = _book(b)
            if it is not None:
                yield it
        cursor = res.get("nextPageCursor")
        if not cursor:
            return
    yield base.PARTIAL                   # more pages than we read: do not prune


def _hl_payload(p: dict) -> dict[str, Any]:
    text = p.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 8191:
        raise base.TokenError("bad_text", "text is required (<= 8191 chars)")
    out: dict[str, Any] = {"text": text}
    for k, lim in (("title", 511), ("author", 1024), ("note", 8191)):
        v = p.get(k)
        if v is not None:
            if not isinstance(v, str) or len(v) > lim:
                raise base.TokenError(f"bad_{k}", f"{k} must be a string (<= {lim})")
            out[k] = v
    src = p.get("source_url")
    if src is not None:
        if not isinstance(src, str) or not src.startswith("https://") or len(src) > 2047:
            raise base.TokenError("bad_source_url", "source_url must be an https URL")
        out["source_url"] = src
    out["source_type"] = "conductloop"
    return out


def create_highlight(c: base.TokenClient, payload: dict) -> dict:
    res = c.post_json(f"{API}/highlights/", {"highlights": [_hl_payload(payload)]})
    books = res if isinstance(res, list) else []
    return {"books": [{"id": b.get("id"), "title": b.get("title")} for b in books if isinstance(b, dict)]}


def _summary(p: dict) -> str:
    _hl_payload(p)
    where = f" en «{p['title']}»" if p.get("title") else ""
    return f"Readwise: guardar subrayado{where}: «{str(p.get('text'))[:200]}»"


CONNECTOR = base.register_connector(base.TokenConnector(
    spec=registry.ConnectorSpec(
        id="readwise", name="Readwise", auth="token", allowed_hosts=("readwise.io",),
        docs_url="https://readwise.io/access_token",
        actions={
            "sync": registry.ActionSpec("sync", "read", "Import books and highlights into the vault",
                                        base.sync_action("readwise")),
            "create_highlight": registry.ActionSpec("create_highlight", "write", "Save a highlight",
                                                    base.token_action("readwise", create_highlight), summarize=_summary),
        }),
    token_mode="keychain", auth_scheme="Token", whoami=whoami, sync=sync, snapshot_kinds=("book",),
    token_help_es="Pega tu token de acceso (readwise.io/access_token). Se guarda solo en el llavero. No se puede limitar a lectura: las escrituras siguen desactivadas aquí hasta que las permitas, y cada una espera tu confirmación.",
    token_help_en="Paste your access token (readwise.io/access_token). It is stored only in the keychain. It can't be limited to read-only: writes stay off here until you allow them, and each one waits for your confirmation.",
))
