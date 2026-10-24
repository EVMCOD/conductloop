"""Obsidian vault indexer.

Walks the Obsidian vault, parses markdown, extracts:
  - title (first H1 or filename)
  - tags (#tag and frontmatter tags)
  - wikilinks [[X]]
  - frontmatter (YAML-ish; we parse conservatively)
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import db

VAULT_DEFAULT = db.vault_root()
SKIP_DIRS = {".obsidian", ".trash", "attachments", ".git", "node_modules"}

H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
TAG_RE = re.compile(r"(?:^|\s)#([a-zA-Z0-9_\-/\.]+)")
WIKILINK_RE = re.compile(r"\[\[([^\]\|]+)(?:\|[^\]]+)?\]\]")
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
KEY_VALUE_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.+?)\s*$", re.MULTILINE)


def _extract_title(text: str, fallback: str) -> str:
    m = H1_RE.search(text)
    return m.group(1).strip() if m else fallback


FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _strip_code(text: str) -> str:
    """The text without the code.

    A ``#include`` or a ``#define`` inside a snippet is code, not a tag.
    Counting them as tags put "include" and "define" among the themes of
    the vault, which is noise that then shows up in the map. 2026-10-23.
    """
    return INLINE_CODE_RE.sub(" ", FENCED_CODE_RE.sub(" ", text))


def _extract_tags(text: str, frontmatter: dict | None) -> list[str]:
    tags: list[str] = []
    if frontmatter:
        fm_tags = frontmatter.get("tags")
        if isinstance(fm_tags, list):
            tags.extend(str(t).lstrip("#") for t in fm_tags)
        elif isinstance(fm_tags, str):
            tags.extend(t.strip().lstrip("#") for t in fm_tags.split(","))
    seen = set()
    for m in TAG_RE.finditer(_strip_code(text)):
        t = m.group(1).strip().rstrip(".,;:")
        if t and t not in seen and not t.startswith("http"):
            seen.add(t)
            tags.append(t)
    return tags


def _extract_wikilinks(text: str) -> list[str]:
    return list({m.group(1).strip() for m in WIKILINK_RE.finditer(text)})


def _parse_frontmatter(text: str) -> tuple[dict | None, str]:
    """Very small YAML-ish parser. Returns (frontmatter_dict, body)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    fm_text = m.group(1)
    body = text[m.end():]
    fm: dict = {}
    for kv in KEY_VALUE_RE.finditer(fm_text):
        k, v = kv.group(1), kv.group(2).strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            fm[k] = [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
        else:
            fm[k] = v.strip('"').strip("'")
    return fm, body


def index_path(path: Path) -> dict:
    """Index a single markdown file. Returns counters for the run."""
    counters = {"indexed": 0, "skipped": 0, "errors": 0}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        counters["errors"] += 1
        print(f"  ! read error {path}: {e}")
        return counters

    try:
        frontmatter, body = _parse_frontmatter(text)
        title = _extract_title(body, path.stem)
        tags = _extract_tags(body, frontmatter)
        wikilinks = _extract_wikilinks(body)

        # Use body (without frontmatter) as content for cleaner search hits.
        content = body if body.strip() else text

        stat = path.stat()
        metadata = {
            "tags": tags,
            "wikilinks": wikilinks[:50],
            "frontmatter_keys": list(frontmatter.keys()) if frontmatter else [],
            # The values too: a note that says "project: deepwick" in its
            # frontmatter is telling us which project it belongs to, and
            # that is the strongest signal there is (2026-10-23).
            "frontmatter": {k: v for k, v in (frontmatter or {}).items()},
        }

        was_upserted = db.upsert_document(
            source="obsidian",
            path=str(path),
            title=title,
            content=content,
            metadata=metadata,
            mtime=int(stat.st_mtime),
            size=stat.st_size,
            # The metadata is part of what this indexer produces, so a file
            # whose *tags* changed has to be re-indexed even when the file
            # itself did not change: otherwise a fixed parser leaves the old
            # reading in the index for ever (2026-10-23).
            compare_content=True,
        )
        if was_upserted:
            counters["indexed"] += 1
        else:
            counters["skipped"] += 1
    except Exception as e:
        counters["errors"] += 1
        print(f"  ! parse error {path}: {e}")
    return counters


def run(vault_root: Path | None = None) -> dict:
    """Index one vault, or (no argument) the write vault plus the read-only
    folders set in the app (library.index_folders)."""
    if vault_root is None:
        from ..library import index_folders
        total = {"indexed": 0, "skipped": 0, "errors": 0}
        for root in [db.vault_root(), *index_folders()]:
            for k, v in _run_one(root).items():
                total[k] += v
        return total
    return _run_one(vault_root)


def _run_one(vault_root: Path) -> dict:
    if not vault_root.exists():
        print(f"vault not found: {vault_root}")
        return {"indexed": 0, "skipped": 0, "errors": 0}

    run_id = db.start_index_run("obsidian")
    total = {"indexed": 0, "skipped": 0, "errors": 0}
    md_files = list(vault_root.rglob("*.md"))
    print(f"obsidian: scanning {len(md_files)} markdown files under {vault_root}")
    seen: set[str] = set()

    # The map the product writes names every project; indexing it would put
    # one synthetic note among the real ones (2026-10-24). The same goes for
    # the pages of the map, one per project: they are the map, not the work.
    from ..library import map_files, map_folders

    skip_files = map_files()
    skip_folders = map_folders()

    for i, path in enumerate(md_files, 1):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        resolved = path.resolve()
        if str(resolved) in skip_files:
            continue
        if any(str(parent) in skip_folders for parent in resolved.parents):
            continue
        seen.add(str(path))
        c = index_path(path)
        for k, v in c.items():
            total[k] += v
        if i % 100 == 0:
            print(f"  [{i}/{len(md_files)}] indexed={total['indexed']} skipped={total['skipped']}")

    # The notes that are no longer in the folder go away with it: a deleted
    # note must not keep answering questions from the index (2026-10-23).
    #
    # The folder existed and was read without failures, so what is missing
    # from it is really missing. (A folder that is not there — a disk that
    # is not mounted — returned above and never prunes anything.)
    removed = 0
    if not total["errors"]:
        removed = db.prune_documents(str(vault_root), seen)
        if removed:
            print(f"  removed {removed} notes that are no longer in the vault")

    db.end_run(run_id, **total, notes=f"vault={vault_root} files={len(md_files)}")
    print(f"obsidian: done. indexed={total['indexed']} skipped={total['skipped']} errors={total['errors']}")
    return total
