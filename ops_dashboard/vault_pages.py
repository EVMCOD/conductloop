"""The map, one page per thing, in the shape a vault understands.

``PROJECTS.md`` is good for a machine and blind for a vault: Obsidian does
not see links in it, so there is no graph, no backlinks, and an agent has to
read 39 KB to find out about one project.

This writes the same map as a folder of pages:

    ConductLoop/pages/Index.md          the map of content
    ConductLoop/pages/<key>.md          one page per project
    ConductLoop/pages/areas/<area>.md   one page per area of loose notes

Every link is a wikilink, and every link to a note of the user carries the
path from the root of the vault: Obsidian resolves those exactly, and it
rewrites them itself when the note is renamed. Our own pages are linked
the same way, so the map is a graph and not a list.

The pages belong to the product: each one carries a marker, and only the
files with that marker are ever rewritten or removed. Everything else in
the folder belongs to the user.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from . import db

log = logging.getLogger("conductloop.vault")

PAGES_DIRNAME = "pages"
AREAS_DIRNAME = "areas"
INDEX_NAME = "Index.md"
PAGE_MARKER = "<!-- conductloop:page -->"

# \w keeps the letters of every alphabet: a key with an accent is still a
# readable filename, and the ones that are not letters become dashes.
_SLUG_RE = re.compile(r"[^\w]+", re.UNICODE)


def slug(text: str) -> str:
    """A filename that is safe on every disk and still readable."""
    out = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return out or "untitled"


def vault_root(path: Path) -> Path | None:
    """The vault a path belongs to: the folder that holds ``.obsidian``.

    That marker is the only honest way to know the path Obsidian expects
    in a link. A folder without it is just a folder: the notes there are
    written as paths, not as links, because a link to nowhere is worse
    than a path that works everywhere.
    """
    try:
        current = Path(path).expanduser().resolve()
    except OSError:
        return None
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        try:
            if (candidate / ".obsidian").is_dir():
                return candidate
        except OSError:
            continue
    return None


def _relative(target: Path, root: Path) -> str | None:
    """The path of a file from the root of the vault, as Obsidian writes it."""
    try:
        rel = Path(target).expanduser().resolve().relative_to(root)
    except (ValueError, OSError):
        return None
    return rel.with_suffix("").as_posix()


class Links:
    """The links of a page, knowing the vault it lives in."""

    def __init__(self, vault: Path | None, here: Path):
        self.vault = vault
        self.here = here

    def note(self, path: str | Path, label: str) -> str:
        """A link to a note of the user: a wikilink when it is in the vault."""
        target = Path(path)
        if self.vault is not None:
            rel = _relative(target, self.vault)
            if rel is not None:
                return f"[[{rel}|{label}]]"
        return f"{label} — `{target}`"

    def page(self, path: Path, label: str) -> str:
        """A link to one of our own pages."""
        if self.vault is not None:
            rel = _relative(path, self.vault)
            if rel is not None:
                return f"[[{rel}|{label}]]"
        try:
            rel = os.path.relpath(path, self.here.parent)
        except ValueError:
            rel = path.name
        return f"[{label}]({Path(rel).as_posix()})"


# ── the notes ─────────────────────────────────────────────────────────────

def _documents() -> list[dict[str, Any]]:
    """Every indexed note, with the project it was attributed to."""
    with db.db() as conn:
        rows = conn.execute(
            "SELECT path, title, project_key, mtime, metadata FROM documents ORDER BY mtime DESC"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        path = str(row["path"])
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        out.append({
            "path": path,
            "title": str(row["title"] or Path(path).stem),
            "project_key": str(row["project_key"] or ""),
            "mtime": int(row["mtime"] or 0),
            "frontmatter": metadata.get("frontmatter") or {},
        })
    return out


def _stamp(mtime: int) -> str:
    """The day a note was last touched, in a form a machine can read."""
    if not mtime:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(mtime))


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """'1 note', '3 notes'."""
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


def _group(documents: list[dict[str, Any]], areas: bool) -> dict[str, list[dict[str, Any]]]:
    """The notes of every project, or of every area of loose notes."""
    from . import notes_structure

    out: dict[str, list[dict[str, Any]]] = {}
    if not areas:
        for doc in documents:
            if doc["project_key"]:
                out.setdefault(doc["project_key"], []).append(doc)
        return out

    roots = notes_structure._vault_roots()
    for doc in documents:
        if doc["project_key"]:
            continue
        area = notes_structure._area_of(doc["path"], roots)
        doc["area"] = area
        out.setdefault(area, []).append(doc)
    return out


# ── the handoffs ───────────────────────────────────────────────────────────

_DATE_IN_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_BLOCKQUOTE = re.compile(r"^>\s?(.*)$")


def _is_handoff(doc: dict[str, Any]) -> bool:
    """Is this note a handoff — "here is where I left off"?

    Two conventions say so: ``type: handoff`` in the frontmatter, and the
    folder the vault keeps them in. Either one is enough.
    """
    front = doc.get("frontmatter") or {}
    if str(front.get("type") or "").strip().lower() == "handoff":
        return True
    parts = Path(doc.get("path") or "").parts
    return any(part.lower() in ("handoff", "handoffs") for part in parts)


def _handoff_date(doc: dict[str, Any]) -> str:
    """The day of the handoff: the frontmatter, the name, or the file."""
    front = doc.get("frontmatter") or {}
    for value in (front.get("date"), front.get("created")):
        text = str(value or "").strip()[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            return text
    match = _DATE_IN_NAME.match(Path(doc.get("path") or "").stem)
    if match:
        return match.group(1)
    return _stamp(int(doc.get("mtime") or 0))


def _encargo(content: str) -> str:
    """The ask of a handoff: the first blockquote under the title.

    In Enrique's handoffs that blockquote is the encargo — what was
    actually asked, in his words. It is the one line that says what the
    work is, and it is worth more than the rest of the note.
    """
    quote: list[str] = []
    for line in (content or "").splitlines():
        match = _BLOCKQUOTE.match(line.strip())
        if match:
            text = match.group(1).strip()
            # "[!success] El encargo" is a callout label, not the ask.
            if text.startswith("[!"):
                continue
            if text:
                quote.append(text)
        elif quote:
            break
    text = " ".join(quote).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > 320:
        text = text[:317].rstrip() + "…"
    return text


def _handoffs(documents: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The handoffs of every project, newest first.

    The content of the note is only read for the ones that are handoffs:
    the vault is a thousand notes and most of them are not.
    """
    candidates = [doc for doc in documents if _is_handoff(doc)]
    if not candidates:
        return {}

    contents: dict[str, str] = {}
    try:
        with db.db() as conn:
            for doc in candidates:
                row = conn.execute(
                    "SELECT content FROM documents WHERE path = ?", (doc["path"],)
                ).fetchone()
                contents[doc["path"]] = str(row["content"] or "") if row else ""
    except Exception as e:
        log.warning("could not read the handoffs: %s", e)

    out: dict[str, list[dict[str, Any]]] = {}
    for doc in candidates:
        key = doc.get("project_key") or ""
        if not key:
            continue
        front = doc.get("frontmatter") or {}
        out.setdefault(key, []).append({
            "path": doc["path"],
            "title": str(front.get("title") or doc.get("title") or ""),
            "date": _handoff_date(doc),
            "encargo": _encargo(contents.get(doc["path"], "")),
        })
    for rows in out.values():
        rows.sort(key=lambda row: row["date"], reverse=True)
    return out


# ── the pages ─────────────────────────────────────────────────────────────

def _header(kind: str, key: str, title: str, intro: str, facts: list[str]) -> list[str]:
    """The top of a page: frontmatter, marker, title, one line of truth."""
    lines = [
        "---",
        "type: conductloop-map",
        f"{kind}: {key}",
        "---",
        PAGE_MARKER,
        f"# {title}",
        "",
        f"> {intro}",
        "",
    ]
    facts = [f for f in facts if f]
    if facts:
        lines.append(" · ".join(facts))
        lines.append("")
    return lines


def _project_page(proj: dict[str, Any], notes: list[dict[str, Any]], *,
                  vault: Path | None, page_path: Path, index_path: Path,
                  unlinked: dict[str, list[dict[str, Any]]],
                  handoffs: list[dict[str, Any]]) -> str:
    """One project: its notes, its repos, and what its page is missing."""
    links = Links(vault, page_path)
    key = proj["key"]
    facts = [_plural(proj["notes"], "note")]
    if proj.get("repos"):
        facts.append(_plural(len(proj["repos"]), "repo"))
    if proj.get("parent"):
        facts.append(f"under {proj['parent']}")
    if not proj.get("known"):
        facts.append("not in the registry yet")

    lines = _header(
        "project", key, proj["name"],
        "The notes that belong to this project, and what the map does not know "
        "yet. Written by ConductLoop from your notes — it is rewritten when "
        "they move.",
        facts)
    lines.append(links.page(index_path, "← the map"))
    lines.append("")

    if handoffs:
        lines.append("## Where this stands")
        lines.append("")
        lines.append("What the last person who worked on this wrote down. Start here.")
        lines.append("")
        for row in handoffs[:5]:
            when = row["date"] or "—"
            lines.append(f"- **{when}** — {links.note(row['path'], row['title'] or 'Handoff')}")
            if row.get("encargo"):
                lines.append(f"  > {row['encargo']}")
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    if notes:
        for note in notes:
            stamp = _stamp(note["mtime"])
            suffix = f" · {stamp}" if stamp else ""
            lines.append(f"- {links.note(note['path'], note['title'])}{suffix}")
    else:
        lines.append("_No note mentions this project yet._")
    lines.append("")

    if proj.get("repos"):
        lines.append("## Repos")
        lines.append("")
        for repo in proj["repos"]:
            lines.append(f"- `{repo}`")
        lines.append("")

    mine = unlinked.get(key) or []
    if mine:
        lines.append("## Notes this page does not link")
        lines.append("")
        for row in mine:
            orphans = ", ".join(row.get("orphans") or [])
            lines.append(f"- **{row.get('title') or key}** — {row.get('count', 0)} note(s) "
                         f"in its folder that its own page does not link: {orphans}")
        lines.append("")

    lines.append("_[This page is written by ConductLoop. Do not edit it by hand.]_")
    return "\n".join(lines) + "\n"


def _area_page(area: str, notes: list[dict[str, Any]], *,
               vault: Path | None, page_path: Path, index_path: Path) -> str:
    """One area of the notes that belong to no project yet."""
    links = Links(vault, page_path)
    lines = _header(
        "area", slug(area), area,
        "Notes that belong to no project yet: ideas until a project claims "
        "them. Written by ConductLoop — do not edit by hand.",
        [_plural(len(notes), "note")])
    lines.append(links.page(index_path, "← the map"))
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    for note in notes:
        stamp = _stamp(note["mtime"])
        suffix = f" · {stamp}" if stamp else ""
        lines.append(f"- {links.note(note['path'], note['title'])}{suffix}")
    lines.append("")
    lines.append("_[This page is written by ConductLoop. Do not edit it by hand.]_")
    return "\n".join(lines) + "\n"


def _index_page(data: dict[str, Any], projects_out: list[dict[str, Any]],
                areas_out: list[dict[str, Any]], *, vault: Path | None,
                index_path: Path,
                handoffs: dict[str, list[dict[str, Any]]]) -> str:
    """The map of content: every page, linked."""
    links = Links(vault, index_path)
    totals = data.get("totals") or {}
    lines = _header(
        "map", "index", "The map of your work",
        "One page per project, one page per area of ideas. Every note of your "
        "vault is linked from the page it belongs to. Written by ConductLoop — "
        "do not edit by hand.",
        [_plural(totals.get("documents", 0), "note"),
         _plural(len(projects_out), "project"),
         _plural(len(areas_out), "area")])
    lines.append("")

    # Where to start: the projects somebody wrote a handoff about, newest
    # first. This is the answer to "what was I doing" without reading a
    # thousand notes.
    started = []
    for item in projects_out:
        rows = handoffs.get(item["key"]) or []
        if not rows:
            continue
        newest = rows[0]
        started.append((newest.get("date") or "", item, newest))
    started.sort(key=lambda row: row[0], reverse=True)
    if started:
        lines.append("## Where to start")
        lines.append("")
        lines.append("The projects somebody left a handoff about, newest first.")
        lines.append("")
        for when, item, newest in started[:8]:
            page = index_path.parent / f"{item['file']}.md"
            lines.append(f"- **{when}** — {links.page(page, item['name'])}: "
                         f"{newest.get('title') or 'Handoff'}")
        lines.append("")

    if projects_out:
        lines.append("## Projects")
        lines.append("")
        for item in projects_out:
            page = index_path.parent / f"{item['file']}.md"
            lines.append(f"- {links.page(page, item['name'])} — "
                         f"{_plural(item['notes'], 'note')}")
        lines.append("")

    if areas_out:
        lines.append("## Ideas (belong to no project yet)")
        lines.append("")
        for item in areas_out:
            page = index_path.parent / AREAS_DIRNAME / f"{item['file']}.md"
            lines.append(f"- {links.page(page, item['area'])} — "
                         f"{_plural(item['count'], 'note')}")
        lines.append("")

    drift = data.get("drift") or {}
    missing = drift.get("missing_from_index") or []
    broken = drift.get("links_to_nothing") or []
    if missing or broken:
        lines.append("## Where your index and the vault disagree")
        lines.append("")
        if missing:
            lines.append(f"- {_plural(len(missing), 'project note')} in no index — "
                         "the index is written by hand and it drifts")
        if broken:
            lines.append(f"- {_plural(len(broken), 'link')} pointing to nothing")
        lines.append("")

    unlinked = data.get("unlinked") or []
    if unlinked:
        lines.append("## Notes a project page does not link")
        lines.append("")
        for row in unlinked:
            lines.append(f"- **{row.get('title') or row.get('project')}** — "
                         f"{row.get('count', 0)} note(s) in its folder that its own "
                         "page does not link")
        lines.append("")

    lines.append("_[This page is written by ConductLoop. Do not edit it by hand.]_")
    return "\n".join(lines) + "\n"


# ── writing ───────────────────────────────────────────────────────────────

def _write_if_changed(path: Path, text: str) -> bool:
    """Write a page only when it changed: the vault does not need noise."""
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        log.warning("could not write the page %s: %s", path, e)
        return False
    return True


def _clean(folder: Path, keep: set[str]) -> list[str]:
    """Remove the pages we wrote that are no longer in the map.

    Only the files that carry our marker: the folder is inside the vault
    of the user and everything else in it is theirs.
    """
    removed: list[str] = []
    if not folder.is_dir():
        return removed
    for path in sorted(folder.glob("*.md")):
        if path.name in keep:
            continue
        try:
            head = path.read_text(encoding="utf-8")[:600]
        except (OSError, UnicodeDecodeError):
            continue
        if PAGE_MARKER not in head:
            continue
        try:
            path.unlink()
            removed.append(path.name)
        except OSError as e:
            log.warning("could not remove the stale page %s: %s", path, e)
    return removed


def _unique(stems: list[str]) -> dict[str, str]:
    """A filename for every key, and no two keys with the same filename."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for stem in stems:
        name = slug(stem)
        if name in taken:
            i = 2
            while f"{name}-{i}" in taken:
                i += 1
            name = f"{name}-{i}"
        taken.add(name)
        out[stem] = name
    return out


def _unlinked_by_project(unlinked: list[dict[str, Any]],
                         by_project: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Which project each orphan belongs to.

    The row knows the folder of the project but not its key: the key comes
    from the registry, the folder from the vault, and they are not always
    the same word. The notes that are missing are the bridge — they were
    attributed to that project before they stopped being linked.
    """
    stem_to_key: dict[str, str] = {}
    for key, notes in by_project.items():
        for note in notes:
            stem_to_key.setdefault(Path(note["path"]).stem, key)
    out: dict[str, list[dict[str, Any]]] = {}
    for row in unlinked:
        keys = {stem_to_key.get(stem) for stem in row.get("orphans") or []}
        keys.discard(None)
        key = next(iter(keys)) if len(keys) == 1 else row.get("project")
        if not key:
            continue
        out.setdefault(str(key), []).append(row)
    return out


def latest_handoffs() -> dict[str, dict[str, Any]]:
    """The newest handoff of every project: the ask, in their words.

    It is what the daily note needs: what was asked, for the projects that
    moved that day.
    """
    handoffs = _handoffs(_documents())
    return {key: rows[0] for key, rows in handoffs.items() if rows}


def export_pages(*, destinations: list[Path],
                 data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write the map as pages, in every destination.

    The data comes from ``notes_structure.structure()``, which attributes
    the notes before this runs: the pages show what the map believes, not
    what the index said last week.
    """
    from . import notes_structure

    if not destinations:
        return {"folders": [], "pages": 0, "written": 0, "removed": 0}

    data = data if data is not None else notes_structure.structure()
    documents = _documents()
    by_project = _group(documents, areas=False)
    by_area = _group(documents, areas=True)

    names = _unique([p["key"] for p in data.get("projects") or []])
    areas_out = [{"area": area, "count": len(notes)} for area, notes in by_area.items()]
    areas_out.sort(key=lambda item: (-item["count"], item["area"].lower()))
    area_names = _unique([item["area"] for item in areas_out])

    projects_out = []
    for proj in data.get("projects") or []:
        if not proj.get("notes"):
            continue
        item = dict(proj)
        item["file"] = names[proj["key"]]
        projects_out.append(item)
    for item in areas_out:
        item["file"] = area_names[item["area"]]

    unlinked = _unlinked_by_project(list(data.get("unlinked") or []), by_project)
    handoffs = _handoffs(documents)

    out: dict[str, Any] = {"folders": [], "pages": 0, "written": 0, "removed": 0}
    for destination in destinations:
        folder = destination / PAGES_DIRNAME
        index_path = folder / INDEX_NAME
        vault = vault_root(destination)
        written = 0

        index_text = _index_page(data, projects_out, areas_out,
                                 vault=vault, index_path=index_path,
                                 handoffs=handoffs)
        if _write_if_changed(index_path, index_text):
            written += 1

        keep: set[str] = {INDEX_NAME}
        for proj in projects_out:
            key = proj["key"]
            page = folder / f"{proj['file']}.md"
            text = _project_page(proj, by_project.get(key, []), vault=vault,
                                 page_path=page, index_path=index_path,
                                 unlinked=unlinked,
                                 handoffs=handoffs.get(key) or [])
            if _write_if_changed(page, text):
                written += 1
            keep.add(page.name)

        areas_folder = folder / AREAS_DIRNAME
        keep_areas: set[str] = set()
        for item in areas_out:
            area = item["area"]
            page = areas_folder / f"{item['file']}.md"
            text = _area_page(area, by_area.get(area, []), vault=vault,
                              page_path=page, index_path=index_path)
            if _write_if_changed(page, text):
                written += 1
            keep_areas.add(page.name)

        removed = _clean(folder, keep) + _clean(areas_folder, keep_areas)
        out["written"] += written
        out["removed"] += len(removed)
        out["pages"] += len(projects_out) + len(areas_out) + 1
        out["folders"].append({"path": str(folder), "written": written,
                               "removed": len(removed)})
    return out
