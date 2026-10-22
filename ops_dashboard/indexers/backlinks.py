"""Backlinks detection — scan the Obsidian vault for references between notes.

Detects three kinds of links:
1. Wiki-style [[Page Name]] (Obsidian standard)
2. Markdown [text](path/to/file.md) links
3. Mention by title: any time another note's title (or filename stem) appears
   in the body of a note, we count it as a mention

For each doc, builds the set of incoming (backlinks) and outgoing links.
Writes to `note_links` table with link_type ('wiki', 'markdown', or 'mention').

CLI:
  python -m ops_dashboard.cli index backlinks              # all docs
  python -m ops_dashboard.cli index backlinks --doc <id>  # one doc only
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .. import db

WIKI_LINK_RE = re.compile(r"\[\[([^\]\n]+?)\]\]")  # [[Page Name]] or [[Page Name|alias]]
MD_LINK_RE = re.compile(r"(?<!\!)\[[^\]\n]*?\]\(([^)\n]+?\.md(?:#[^\s)]*)?)\)")  # [text](file.md) — group 1 = URL


def _normalize_target(name: str) -> str:
    """Normalize a wiki target for fuzzy matching against doc titles/paths.
    'Project Alpha|alias' -> 'project alpha'
    'PROJ-123/foo.md' -> 'foo' """
    name = name.split("|")[0].strip()
    name = name.split("#")[0].strip()
    # Strip path components
    name = name.rstrip("/").split("/")[-1] if "/" in name else name
    name = name.removesuffix(".md") if name.lower().endswith(".md") else name
    return name.lower().strip()


def _build_index() -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """Build lookup dicts: title -> [doc_ids], filename_stem -> [doc_ids]."""
    title_to_ids: dict[str, list[int]] = {}
    stem_to_ids: dict[str, list[int]] = {}
    for d in db.recent_documents(limit=20000, source=None):
        title = (d.get("title") or "").lower().strip()
        if title:
            title_to_ids.setdefault(title, []).append(d["id"])
        path = d.get("path") or ""
        stem = Path(path).stem.lower().strip()
        if stem:
            stem_to_ids.setdefault(stem, []).append(d["id"])
    return title_to_ids, stem_to_ids


def detect_links_for_doc(doc_id: int, title_to_ids: dict, stem_to_ids: dict) -> list[dict]:
    """Detect all links FROM doc_id to other docs. Returns [{target_doc_id, type, context}]."""
    with db.db() as conn:
        row = conn.execute("SELECT id, title, path, content FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        return []
    content = row["content"] or ""
    title = (row["title"] or "").strip()
    path = row["path"] or ""
    source_stem = Path(path).stem.lower().strip() if path else ""
    source_title = title.lower().strip() if title else ""

    found: dict[tuple[int, str], str] = {}  # (target_id, type) -> context snippet

    # 1) Wiki-style [[links]]
    for m in WIKI_LINK_RE.finditer(content):
        target = _normalize_target(m.group(1))
        if not target or target == source_stem or target == source_title:
            continue
        # Try title first, then stem
        targets = title_to_ids.get(target, []) or stem_to_ids.get(target, [])
        if not targets:
            continue
        # Pick best match (prefer first doc; could later add smart disambiguation)
        target_id = targets[0]
        snippet = content[max(0, m.start() - 40):min(len(content), m.end() + 40)].strip()
        found[(target_id, "wiki")] = snippet

    # 2) Markdown [text](file.md) links
    for m in MD_LINK_RE.finditer(content):
        target_raw = m.group(1).strip()
        target = _normalize_target(target_raw)
        if not target or target == source_stem:
            continue
        targets = stem_to_ids.get(target, []) or title_to_ids.get(target, [])
        if not targets:
            continue
        target_id = targets[0]
        snippet = content[max(0, m.start() - 40):min(len(content), m.end() + 40)].strip()
        found[(target_id, "markdown")] = snippet

    # 3) Plain mentions by title (sub-string search, word-boundary to avoid 'rental' matching 'rent')
    if source_title and len(source_title) > 3:
        # Search for this doc's title in OTHER docs' content (handled separately in run())
        pass

    return [{"target_doc_id": t, "link_type": typ, "context": ctx} for (t, typ), ctx in found.items()]


def _scan_mentions(source_doc_id: int, content: str, title_to_ids: dict, stem_to_ids: dict,
                    source_title: str, source_stem: str, *, max_candidates: int = 200) -> list[dict]:
    """Find documents that MENTION source_doc (by title or stem in their content).

    Performance: limit candidate set to first N obsidian docs by mtime. The full
    scan over 2000+ docs is too slow for a sync index. Better to do this async
    later or chunked.
    """
    if not source_title and not source_stem:
        return []
    needles: list[tuple[str, str]] = []
    if source_title and len(source_title) > 3:
        needles.append((source_title, "mention_title"))
    if source_stem and len(source_stem) > 3 and source_stem != source_title:
        needles.append((source_stem, "mention_stem"))
    if not needles:
        return []

    # Pre-fetch candidate docs (limited, ordered by mtime to favor recent)
    with db.db() as conn:
        candidates = conn.execute(
            "SELECT id, content, title FROM documents WHERE source = 'obsidian' "
            "AND id != ? ORDER BY mtime DESC LIMIT ?",
            (source_doc_id, max_candidates),
        ).fetchall()

    found: dict[tuple[int, str], str] = {}
    for row in candidates:
        body = row["content"] or ""
        body_l = body.lower()
        for needle, n_type in needles:
            nl = needle.lower()
            # Word boundary check
            if not re.search(r"\b" + re.escape(nl) + r"\b", body_l):
                continue
            m = re.search(r"\b" + re.escape(nl) + r"\b", body_l)
            snippet = body[max(0, m.start() - 40):min(len(body), m.end() + 40)].strip()
            found[(row["id"], n_type)] = snippet
    return [{"target_doc_id": source_doc_id, "link_type": typ, "context": ctx} for (cid, typ), ctx in found.items()]


def run(doc_id: int | None = None) -> dict:
    """Scan all docs (or one) for links. Returns counts."""
    title_to_ids, stem_to_ids = _build_index()
    now = int(time.time())
    # Get docs to process
    if doc_id:
        docs = [d for d in db.recent_documents(limit=1, source=None) if d["id"] == doc_id]
        if not docs:
            return {"scanned": 0, "links": 0, "by_type": {}}
    else:
        docs = db.recent_documents(limit=20000, source=None)
    total_links = 0
    by_type: dict[str, int] = {}
    # Clear existing links for the affected docs first (idempotent rebuild)
    affected_ids = [d["id"] for d in docs]
    with db.db() as conn:
        for did in affected_ids:
            conn.execute("DELETE FROM note_links WHERE source_doc_id = ?", (did,))
            # Also remove incoming links that would change (links whose target is this doc)
            # -- skip: we rebuild those separately below
    # Build outgoing links
    for d in docs:
        links = detect_links_for_doc(d["id"], title_to_ids, stem_to_ids)
        for link in links:
            try:
                db.add_note_link(d["id"], link["target_doc_id"], link["link_type"], link["context"], now)
                total_links += 1
                by_type[link["link_type"]] = by_type.get(link["link_type"], 0) + 1
            except Exception:
                pass
    # Build incoming links (mentions)
    for d in docs:
        title = (d.get("title") or "").strip()
        path = d.get("path") or ""
        stem = Path(path).stem.strip() if path else ""
        with db.db() as conn:
            row = conn.execute("SELECT content FROM documents WHERE id = ?", (d["id"],)).fetchone()
        content = row["content"] if row and row["content"] else ""
        mentions = _scan_mentions(d["id"], content, title_to_ids, stem_to_ids,
                                  title.lower(), stem.lower())
        for m in mentions:
            try:
                db.add_note_link(m["target_doc_id"], d["id"], m["link_type"], m["context"], now)
                total_links += 1
                by_type[m["link_type"]] = by_type.get(m["link_type"], 0) + 1
            except Exception:
                pass
    # Log
    try:
        db.add_activity("backlinks_scan", f"Indexed {len(docs)} docs, {total_links} links ({', '.join(f'{k}={v}' for k, v in by_type.items())})")
    except Exception:
        pass
    return {"scanned": len(docs), "links": total_links, "by_type": by_type}
