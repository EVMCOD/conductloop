"""Google Drive connector — read + ingest (this PR).

Scope (read): ``drive.readonly`` — **restricted** per Google. The non-sensitive
``drive.file`` only covers files the app created or the user picked with the
Google Picker, which a local app does not have; reading your existing docs
needs ``drive.readonly`` (see the guide on /connectors).

Actions:
* ``list_files`` (read) — name, type, modified time and link (no content).
* ``get_file`` (read) — one file as text: Google Docs/Slides exported as
  ``text/plain``, Sheets as zipped HTML (one page per worksheet; CSV export
  only carries the first sheet) turned into CSV, and plain-text files (txt, md, csv,
  json) downloaded if under 2 MB. Anything else (PDF, images, binaries) is
  metadata only — never downloaded.
* ``sync`` (read) — files modified in the last N days (default 30, max 100
  files) into the vault (``documents``, source ``google_drive``).
"""
from __future__ import annotations

import csv
import io
import re
import time
import zipfile
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from .. import connector_registry as registry
from . import _google, _ingest

ID = "google_drive"
API = "https://www.googleapis.com/drive/v3"
READ_SCOPES = (_google.scope("drive.readonly"),)
MAX_DOWNLOAD = 2 * 1024 * 1024
MAX_SYNC = 100
_FIELDS = "nextPageToken,files(id,name,mimeType,modifiedTime,size,webViewLink,trashed)"
EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "application/zip",
}
MAX_SHEETS = 50
MAX_UNZIPPED = 10 * 1024 * 1024
DOWNLOAD = ("text/plain", "text/markdown", "text/csv", "application/json", "text/x-markdown")


def _ts(s: Any) -> int | None:
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _safe_id(v: Any) -> str:
    s = str(v or "")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", s):
        raise ValueError("bad file id")
    return s


def _q_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _files(client: Any, q: str, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    token = None
    while len(out) < limit:
        params: dict[str, Any] = {"q": q, "fields": _FIELDS, "pageSize": min(100, limit - len(out)),
                                  "orderBy": "modifiedTime desc", "spaces": "drive"}
        if token:
            params["pageToken"] = token
        body = _google.get_json(client, f"{API}/files", params)
        out += [f for f in body.get("files") or [] if isinstance(f, dict) and f.get("id") and not f.get("trashed")]
        token = body.get("nextPageToken")
        if not token:
            break
    return out[:limit]


def _meta(f: dict[str, Any]) -> dict[str, Any]:
    return {"id": f.get("id"), "name": f.get("name"), "mime_type": f.get("mimeType"),
            "modified": _ts(f.get("modifiedTime")), "size": int(f["size"]) if str(f.get("size", "")).isdigit() else None,
            "link": f.get("webViewLink")}


def readable(f: dict[str, Any]) -> bool:
    mime = str(f.get("mimeType") or "")
    if mime in EXPORT:
        return True
    size = int(f["size"]) if str(f.get("size", "")).isdigit() else MAX_DOWNLOAD + 1
    return mime in DOWNLOAD and size <= MAX_DOWNLOAD


class _Table(HTMLParser):
    """Rows of the first <table> in a Sheets HTML export (its row-number
    column and header row of A, B, C… are dropped)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "table":
            self._depth += 1
        elif tag == "tr" and self._depth == 1:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._depth -= 1
        elif tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def sheets_zip_to_text(data: bytes) -> str:
    """All worksheets of a Sheets ``application/zip`` export as CSV blocks."""
    out: list[str] = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        pages = [i for i in z.infolist() if i.filename.lower().endswith(".html")][:MAX_SHEETS]
        for info in pages:
            total += info.file_size
            if total > MAX_UNZIPPED:          # zip bomb / huge workbook: stop, say so
                out.append("[…more worksheets not imported: size limit]")
                break
            parser = _Table()
            parser.feed(z.read(info).decode("utf-8", "replace"))
            rows = parser.rows[1:] if parser.rows and parser.rows[0][1:2] == ["A"] else parser.rows
            rows = [r[1:] if r and (r[0].isdigit() or r[0] == "") else r for r in rows]
            rows = [r for r in rows if any(c for c in r)]
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerows(rows)
            name = info.filename.rsplit("/", 1)[-1][:-5]
            out.append(f"## {name}\n\n{buf.getvalue().strip()}")
    return "\n\n".join(out)


def _content(client: Any, f: dict[str, Any]) -> str | None:
    fid = _safe_id(f.get("id"))
    mime = str(f.get("mimeType") or "")
    if mime in EXPORT:
        r = client.get(f"{API}/files/{fid}/export", params={"mimeType": EXPORT[mime]})
    elif readable(f):
        r = client.get(f"{API}/files/{fid}", params={"alt": "media"})
    else:
        return None
    if r.status_code >= 400:
        raise _google.GoogleAPIError(r.status_code, "content")
    if EXPORT.get(mime) == "application/zip":
        try:
            return sheets_zip_to_text(r.content)[:MAX_DOWNLOAD]
        except zipfile.BadZipFile as e:
            raise _google.GoogleAPIError(502, "bad_sheets_export") from e
    return r.content[:MAX_DOWNLOAD].decode("utf-8", "replace")


def list_files(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    q = "trashed = false"
    name = str(payload.get("name") or "").strip()
    if name:
        q += f" and name contains '{_q_literal(name[:100])}'"
    limit = _ingest.clamp_int(payload, "max", 20, 1, 100)
    return {"files": [_meta(f) for f in _files(client, q, limit)]}


def get_file(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    fid = _safe_id(payload.get("id"))
    f = _google.get_json(client, f"{API}/files/{fid}", {"fields": "id,name,mimeType,modifiedTime,size,webViewLink,trashed"})
    text = _content(client, f)
    return {**_meta(f), "text": None if text is None else text[:50000], "readable": text is not None}


def sync(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    days = _ingest.clamp_int(payload, "days", 30, 1, 365)
    limit = _ingest.clamp_int(payload, "max", 50, 1, MAX_SYNC)
    since = datetime.fromtimestamp(time.time() - days * 86400, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = {"files": 0, "vault_updated": 0, "skipped": 0}
    for f in _files(client, f"trashed = false and modifiedTime > '{since}'", limit):
        stats["files"] += 1
        text = _content(client, f) if readable(f) else None
        if text is None:
            stats["skipped"] += 1
            continue
        m = _meta(f)
        doc = f"# {m['name']}\n\n- Tipo: {m['mime_type']}\n- Enlace: {m['link'] or '-'}\n\n{text}"
        if _ingest.to_vault(source=ID, path=f"google_drive/{m['id']}", title=str(m["name"])[:300], content=doc,
                            mtime=m["modified"], metadata={"kind": "file", "mime_type": m["mime_type"],
                                                           "link": m["link"], "tags": ["drive", "google"]}):
            stats["vault_updated"] += 1
    return stats


SPEC = registry.register(registry.ConnectorSpec(
    id=ID, name="Google Drive",
    allowed_hosts=(*_google.AUTH_HOSTS, "www.googleapis.com"),
    read_scopes=READ_SCOPES,
    actions={
        "list_files": registry.ActionSpec("list_files", "read", "List your Drive files (metadata only)", list_files),
        "get_file": registry.ActionSpec("get_file", "read", "Read one Drive file as text", get_file),
        "sync": registry.ActionSpec("sync", "read", "Import recently modified files into the vault", sync),
    },
    **_google.oauth_kwargs(),
))
_google.join_group(ID)
