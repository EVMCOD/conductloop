"""The map of your work, read from your notes.

When you change agents, the notes are what stays: the vault, the daily
notes, the memory files. They are indexed — but indexed is not the same as
understood. A note that mentions a business belongs to it even when it
sits in a flat folder, and a note that belongs to nobody is an idea
waiting for a home.

This module does three things:

* **attributes** a note to a project by what it *says*, not only by where
  it sits: the frontmatter, the tags, the links and the title all count;
* **groups** the notes that belong to nobody into ideas;
* **proposes** the projects the notes know about and the registry does not.

And it writes the map where the next agent will find it: the point of a
second brain is not that it answers well, it is that it does not matter
which agent is looking at it.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from . import db, projects

log = logging.getLogger("conductloop.notes")

# A needle shorter than this matches too much to be a signal.
MIN_NEEDLE = 3
# A body mention has to repeat before it counts: one word in passing is
# not a project.
BODY_MENTIONS_TO_COUNT = 3
# A folder or tag needs this many loose notes before it is worth suggesting.
SUGGEST_MIN_NOTES = 3

EXPLICIT_FIELDS = ("project", "proyecto", "business", "negocio", "client", "cliente")


# ── the signals ───────────────────────────────────────────────────────────

def _needles(proj: projects.Project) -> list[tuple[str, str]]:
    """Every name a project answers to, longest first."""
    out: list[tuple[str, str]] = []
    for name in [proj.name, proj.key, *proj.aliases]:
        norm = _norm(name)
        if len(norm) >= MIN_NEEDLE and norm not in {n for n, _ in out}:
            out.append((norm, name))
    return sorted(out, key=lambda pair: -len(pair[0]))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _word(needle: str) -> re.Pattern:
    """A needle as a whole word, so "ops" does not match "opsdashboard".

    A hyphen is a separator, not part of a word: "rentcla-launch" is about
    Rentcla.
    """
    return re.compile(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", re.IGNORECASE)


def _doc_metadata(row: Any) -> dict[str, Any]:
    try:
        meta = json.loads(row["metadata"] or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def signals_for(row: Any) -> list[tuple[str, float, str]]:
    """Which projects this document talks about, and how strongly.

    Returns a list of ``(project_key, score, why)``, strongest first.
    """
    meta = _doc_metadata(row)
    title = str(row["title"] or "")
    content = str(row["content"] or "")
    tags = [str(t).lstrip("#").strip() for t in (meta.get("tags") or [])]
    links = [str(w).strip() for w in (meta.get("wikilinks") or [])]
    front = meta.get("frontmatter") if isinstance(meta.get("frontmatter"), dict) else {}

    out: list[tuple[str, float, str]] = []
    for proj in projects.PROJECTS:
        best: tuple[float, str] | None = None
        for needle, original in _needles(proj):
            pattern = _word(needle)
            # 1. The frontmatter says so outright.
            for field in EXPLICIT_FIELDS:
                value = str(front.get(field) or "").strip().lower()
                if value and (value == needle or value in (proj.aliases or [])
                              or value == proj.key.lower()):
                    best = max(best or (0.0, ""), (1.0, f"frontmatter says {original}"))
            # 2. A tag carries it.
            for tag in tags:
                if _norm(tag) == needle:
                    best = max(best or (0.0, ""), (0.95, f"tagged {original}"))
            # 3. A link points at it.
            for link in links:
                if _norm(link) == needle:
                    best = max(best or (0.0, ""), (0.85, f"links to {original}"))
            # 4. The title names it.
            if pattern.search(title):
                best = max(best or (0.0, ""), (0.9, f"titled after {original}"))
            # 5. The body keeps saying it.
            if content and len(pattern.findall(content)) >= BODY_MENTIONS_TO_COUNT:
                best = max(best or (0.0, ""), (0.5, f"mentions {original}"))
        if best:
            out.append((proj.key, best[0], best[1]))
    return sorted(out, key=lambda item: -item[1])


# ── attribution ───────────────────────────────────────────────────────────

def attribute_documents(*, apply: bool = True) -> dict[str, Any]:
    """Give every note a project when the note itself says which one.

    Only rows without a project are touched: an assignment the user made (or
    the path made) always wins over what the text says.
    """
    with db.db() as conn:
        rows = conn.execute(
            "SELECT id, title, content, metadata, path, project_key FROM documents "
            "WHERE project_key IS NULL").fetchall()
    assigned: dict[str, int] = {}
    updates: list[tuple[str, int]] = []
    for row in rows:
        found = signals_for(row)
        if not found:
            continue
        key = found[0][0]
        assigned[key] = assigned.get(key, 0) + 1
        updates.append((key, int(row["id"])))
    if apply and updates:
        with db.db() as conn:
            conn.executemany(
                "UPDATE documents SET project_key=? WHERE id=? AND project_key IS NULL",
                updates)
    return {"considered": len(rows), "assigned": len(updates), "by_project": assigned}


# ── the map ───────────────────────────────────────────────────────────────

def _area_of(path: str, roots: list[Path]) -> str:
    """The area a note belongs to: its first two levels inside the vault.

    The leaf folder is too fine — a vault full of translations and docs has
    "commands" in three places, and none of them is a project. Two levels
    is where a person puts a body of work.
    """
    p = Path(path)
    for root in roots:
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        parts = [part for part in rel.parts[:-1] if part not in (".",)]
        if not parts:
            return "(root)"
        if len(parts) == 1:
            return parts[0]
        return "/".join(parts[:2])
    return p.parent.name or "(root)"


def _vault_roots() -> list[Path]:
    from . import library

    roots = [db.vault_root().expanduser()]
    for folder in library.index_folders():
        roots.append(folder.expanduser())
    return [r for r in roots if r.exists()]


# ── the vault's own convention ────────────────────────────────────────────

_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
_FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_ -]*)\s*:\s*(.+?)\s*$")


def _frontmatter(text: str) -> dict[str, str]:
    """The frontmatter of a note, as plain strings.

    Deliberately not a YAML parser: the values here are a status, a type,
    a tag list. A note that does not fit is not broken by it.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in text[4:end].splitlines():
        m = _FM_KEY_RE.match(line)
        if m:
            out[m.group(1).strip().lower()] = m.group(2).strip().strip("\"'")
    return out


def _link_targets(text: str) -> set[str]:
    """The note names a piece of text links to."""
    out: set[str] = set()
    for m in _WIKILINK_RE.finditer(text):
        target = m.group(1).strip().rstrip("\\")
        if not target or target.startswith("#"):
            continue
        out.add(target.split("/")[-1].removesuffix(".md"))
    return out


def _read_notes(roots: list[Path]) -> list[dict[str, Any]]:
    """Every note of the vault, with what its frontmatter says about it."""
    out: list[dict[str, Any]] = []
    for root in roots:
        for path in root.rglob("*.md"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm = _frontmatter(text)
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = path.name
            out.append({
                "stem": path.stem,
                "rel": rel,
                "path": str(path),
                "frontmatter": fm,
                "type": (fm.get("type") or "").lower(),
                "status": (fm.get("status") or "").lower(),
                "title": fm.get("title") or path.stem,
                "links": _link_targets(text),
                "mtime": int(path.stat().st_mtime),
                "in_projects": rel.startswith("projects/"),
            })
    return out


def index_drift(*, roots: list[Path] | None = None) -> dict[str, Any]:
    """Where the vault's own index and the vault disagree.

    The index is written by hand, so it drifts: a project note appears and
    the index does not mention it, or the index counts thirty-three active
    projects and there are fifty. This reads the convention from the notes
    themselves — a project says what it is in its frontmatter, and the
    index says which notes belong — and reports the difference.
    """
    roots = roots if roots is not None else _vault_roots()
    notes = _read_notes(roots)
    by_stem: dict[str, list[dict[str, Any]]] = {}
    for note in notes:
        by_stem.setdefault(note["stem"], []).append(note)

    # The hubs: the notes that are indexes of other notes.
    hubs = sorted((n for n in notes if n["type"] == "hub"),
                  key=lambda n: -len(n["links"]))

    # A project says so in its frontmatter. The other notes in the projects
    # folder are its documentation — not projects — and they are what the
    # "unlinked" report is about.
    project_notes = [n for n in notes if n["type"] == "project"]
    project_notes.sort(key=lambda n: n["stem"])

    listed: set[str] = set()
    for hub in hubs:
        listed |= hub["links"]

    by_status: dict[str, list[str]] = {}
    for note in project_notes:
        by_status.setdefault(note["status"] or "(no status)", []).append(note["stem"])

    missing = sorted(n["stem"] for n in project_notes if n["stem"] not in listed)
    ghosts = sorted(t for t in listed if t not in by_stem)

    # The ideas: the project notes that are waiting for a decision.
    ideas = sorted(by_status.get("idea", []) + by_status.get("open-decision", []))

    return {
        "notes": len(notes),
        "projects": len(project_notes),
        "by_status": {k: len(v) for k, v in sorted(by_status.items())},
        "hubs": [{"stem": h["stem"], "rel": h["rel"], "links": len(h["links"])}
                 for h in hubs],
        "listed": len(listed),
        "missing_from_index": missing,
        "links_to_nothing": ghosts,
        "ideas": ideas,
        "ideas_not_listed": sorted(i for i in ideas if i not in listed),
    }


def unlinked_notes(*, roots: list[Path] | None = None, limit: int = 12) -> list[dict[str, Any]]:
    """The notes of a project that its own page does not link.

    A project keeps its documentation in its own folder — that is the
    convention — and its page links the notes that matter. The ones the
    page forgot are in the vault, the search finds them, and nobody sees
    them when they open the project.

    (A note that merely mentions the project is not one of its notes: a
    daily note that says "rentcla" is a daily note. What counts is the
    note that lives in the project's own folder.)
    """
    roots = roots if roots is not None else _vault_roots()
    out: list[dict[str, Any]] = []
    for root in roots:
        projects_dir = root / "projects"
        if not projects_dir.is_dir():
            continue
        for page in sorted(projects_dir.glob("*.md")):
            if page.stem == "_index":
                continue
            try:
                text = page.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm = _frontmatter(text)
            if fm.get("type", "").lower() not in ("project", ""):
                continue
            folder = projects_dir / page.stem
            if not folder.is_dir():
                continue
            linked = _link_targets(text)
            orphans = sorted(p.stem for p in folder.glob("*.md") if p.stem not in linked)
            if orphans:
                out.append({
                    "project": page.stem,
                    "title": fm.get("title") or page.stem,
                    "path": str(page),
                    "folder": str(folder),
                    "orphans": orphans[:limit],
                    "count": len(orphans),
                })
    out.sort(key=lambda item: -item["count"])
    return out


def structure(*, attribute: bool = True) -> dict[str, Any]:
    """The map: projects with their notes, ideas with no project, suggestions.

    This is what the page shows and what the export writes.
    """
    if attribute:
        attribute_documents()
    roots = _vault_roots()
    with db.db() as conn:
        rows = conn.execute(
            "SELECT id, source, path, title, content, metadata, project_key, mtime "
            "FROM documents ORDER BY mtime DESC").fetchall()

    by_project: dict[str, list[dict[str, Any]]] = {}
    loose: list[dict[str, Any]] = []
    for row in rows:
        meta = _doc_metadata(row)
        note = {
            "id": int(row["id"]),
            "title": str(row["title"] or Path(row["path"]).stem),
            "path": str(row["path"]),
            "source": str(row["source"]),
            "mtime": int(row["mtime"] or 0),
            "tags": [str(t) for t in (meta.get("tags") or [])][:8],
        }
        if row["project_key"]:
            by_project.setdefault(str(row["project_key"]), []).append(note)
        else:
            note["area"] = _area_of(str(row["path"]), roots)
            loose.append(note)

    out_projects = []
    for proj in projects.PROJECTS:
        notes = by_project.get(proj.key, [])
        out_projects.append({
            "key": proj.key,
            "name": proj.name,
            "parent": proj.parent,
            "notes": len(notes),
            "ids": sorted(n["id"] for n in notes),
            "recent": notes[:5],
            "repos": [str(r) for r in (proj.repo_paths or [])],
            "known": True,
        })
    # Projects that only exist in the notes (registered elsewhere).
    for key, notes in by_project.items():
        if any(p["key"] == key for p in out_projects):
            continue
        out_projects.append({
            "key": key, "name": key, "parent": None, "notes": len(notes),
            "ids": sorted(n["id"] for n in notes),
            "recent": notes[:5], "repos": [], "known": False,
        })

    ideas: dict[str, list[dict[str, Any]]] = {}
    for note in loose:
        ideas.setdefault(note["area"], []).append(note)

    return {
        "projects": sorted(out_projects, key=lambda p: (-p["notes"], p["name"].lower())),
        "drift": index_drift(roots=roots),
        "unlinked": unlinked_notes(roots=roots),
        "ideas": [
            {"area": area, "notes": notes[:50], "count": len(notes),
             "ids": sorted(n["id"] for n in notes)}
            for area, notes in sorted(ideas.items(), key=lambda kv: -len(kv[1]))
        ],
        "suggestions": suggestions(loose),
        "topics": topics(loose),
        "totals": {
            "documents": len(rows),
            "attributed": sum(len(v) for v in by_project.values()),
            "loose": len(loose),
        },
    }


def suggestions(loose: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bodies of work the notes know about and the registry does not.

    A folder with notes in it is a project waiting to be named. The user
    decides; the product only points. (The tags are a different thing —
    topics, not projects — so they are counted apart.)
    """
    known = {p.key.lower() for p in projects.PROJECTS}
    known |= {_norm(a) for p in projects.PROJECTS for a in (p.aliases or [])}
    out: list[dict[str, Any]] = []

    areas: dict[str, int] = {}
    for note in loose:
        areas[note["area"]] = areas.get(note["area"], 0) + 1
    for area, count in areas.items():
        name = Path(area).name
        if count >= SUGGEST_MIN_NOTES and _norm(name) not in known and area not in (".", "", "(root)"):
            out.append({"kind": "area", "name": area, "notes": count,
                        "why": f"{count} notes under {area}"})

    return sorted(out, key=lambda s: -s["notes"])[:12]


def topics(loose: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The themes the loose notes keep coming back to.

    A tag is a subject, not a project: "trading" or "papers" say what a
    note is about. They are worth showing — a theme with two hundred notes
    in it is usually a body of work hiding behind a subject.
    """
    known = {p.key.lower() for p in projects.PROJECTS}
    known |= {_norm(a) for p in projects.PROJECTS for a in (p.aliases or [])}
    counts: dict[str, int] = {}
    for note in loose:
        for tag in note.get("tags") or []:
            if _norm(tag) not in known:
                counts[tag] = counts.get(tag, 0) + 1
    return [{"name": tag, "notes": count, "why": f"{count} notes tagged {tag}"}
            for tag, count in sorted(counts.items(), key=lambda kv: -kv[1])
            if count >= SUGGEST_MIN_NOTES][:12]


# ── the map, written where the next agent looks ───────────────────────────

def export_markdown(structure_map: dict[str, Any] | None = None) -> str:
    """The map as markdown: what an agent can read in one go.

    No date inside: the map is rewritten only when it changes, and a date
    would make it different every time. When it was written, and what moved,
    is in the pointer.
    """
    data = structure_map if structure_map is not None else structure()
    lines = ["# Projects and ideas", "",
             "Generated by ConductLoop from your notes. This is the map: it does not",
             "matter which agent reads it.", ""]
    projects_out = [p for p in data["projects"] if p["notes"]]
    if projects_out:
        lines.append("## Projects")
        lines.append("")
        for proj in projects_out:
            lines.append(f"### {proj['name']} ({proj['key']})")
            lines.append(f"{proj['notes']} notes" + (f" · {len(proj['repos'])} repos" if proj["repos"] else ""))
            lines.append("")
            for note in proj["recent"]:
                lines.append(f"- {note['title']} — {note['path']}")
            lines.append("")
    ideas = data["ideas"]
    if ideas:
        lines.append("## Ideas (not attached to a project)")
        lines.append("")
        for group in ideas:
            lines.append(f"### {group['area']} ({group['count']})")
            for note in group["notes"][:20]:
                lines.append(f"- {note['title']} — {note['path']}")
            lines.append("")
    drift = data.get("drift") or {}
    missing = drift.get("missing_from_index") or []
    broken = drift.get("links_to_nothing") or []
    if missing or broken:
        lines.append("## Where the index and the vault disagree")
        lines.append("")
        if missing:
            lines.append(f"{len(missing)} project notes are not in any index — "
                         "the index is written by hand and it drifts:")
            lines.append("")
            for stem in missing[:25]:
                lines.append(f"- {stem}")
            if len(missing) > 25:
                lines.append(f"- … and {len(missing) - 25} more")
            lines.append("")
        if broken:
            lines.append("Links that point to nothing:")
            lines.append("")
            for stem in broken[:12]:
                lines.append(f"- {stem}")
            lines.append("")
    unlinked = data.get("unlinked") or []
    if unlinked:
        lines.append("## Notes of a project that its page does not link")
        lines.append("")
        for row in unlinked:
            lines.append(f"- **{row['title']}** — {row['count']} note(s) in its folder "
                         f"that its page does not link: {', '.join(row['orphans'][:5])}")
        lines.append("")
    suggestions_out = data.get("suggestions") or []
    if suggestions_out:
        lines.append("## Possible projects (not registered yet)")
        lines.append("")
        for suggestion in suggestions_out:
            lines.append(f"- **{suggestion['name']}** — {suggestion['why']}")
        lines.append("")
    totals = data["totals"]
    lines.append(f"_{totals['documents']} notes · {totals['attributed']} in a project "
                 f"· {totals['loose']} loose_")
    return "\n".join(lines) + "\n"


MAP_FILENAME = "PROJECTS.md"
STATE_FILENAME = "PROJECTS.state.json"
POINTER_BEGIN = "<!-- conductloop:map -->"
POINTER_END = "<!-- /conductloop:map -->"


def _pointer_block(summary: str) -> str:
    return f"""{POINTER_BEGIN}
> **The map of the work** — `{MAP_FILENAME}` in this folder. Which project each
> note belongs to, which ideas belong to nobody yet, and the projects your
> notes know about but the registry does not. Read it before answering
> anything about a project: it is the same for every agent, which is the
> point of writing it down.
>
> {summary}
{POINTER_END}
"""


def fingerprint(structure_map: dict[str, Any]) -> dict[str, Any]:
    """What the map says, in a form two maps can be compared on.

    The ids, not the titles: a note that changed its title is the same note,
    and a map that rewrote itself because of it would be noise.
    """
    return {
        "projects": {p["key"]: list(p.get("ids") or []) for p in structure_map["projects"]},
        "areas": {g["area"]: list(g.get("ids") or []) for g in structure_map["ideas"]},
        "suggestions": sorted(s["name"] for s in structure_map.get("suggestions") or []),
    }


def diff_maps(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """What changed between two maps.

    This is what the agent gets instead of re-reading 39 KB: which project
    gained notes, which are new, and which ones left.

    With no previous map there is nothing to compare against: everything
    would look new, which is true and useless. That case says so.
    """
    if old is None:
        return {
            "added": {}, "removed": {}, "new_projects": [], "gone_projects": [],
            "new_areas": [], "new_suggestions": [], "first": True, "changed": True,
        }
    old = old or {"projects": {}, "areas": {}, "suggestions": []}
    old_projects = old.get("projects") or {}
    new_projects = new.get("projects") or {}
    added: dict[str, int] = {}
    removed: dict[str, int] = {}
    for key, ids in new_projects.items():
        before = set(old_projects.get(key) or [])
        delta = len(set(ids) - before)
        if delta:
            added[key] = delta
    for key, ids in old_projects.items():
        after = set(new_projects.get(key) or [])
        delta = len(after - set(ids))
        if delta:
            removed[key] = delta
    return {
        "added": added,
        "removed": removed,
        "new_projects": sorted(set(new_projects) - set(old_projects)),
        "gone_projects": sorted(set(old_projects) - set(new_projects)),
        "new_areas": sorted(set(new.get("areas") or {}) - set(old.get("areas") or {})),
        "new_suggestions": sorted(set(new.get("suggestions") or []) - set(old.get("suggestions") or [])),
        "changed": bool(added or removed or set(new_projects) != set(old_projects)
                       or set(new.get("areas") or {}) != set(old.get("areas") or {})),
    }


def _names(keys: list[str]) -> dict[str, str]:
    out = {}
    for p in projects.PROJECTS:
        out[p.key] = p.name
    return out


def summarize_change(change: dict[str, Any]) -> str:
    """One line an agent can read before it decides to open the map."""
    if change.get("first"):
        return "Last change: the map has just been drawn."
    if not change.get("changed"):
        return "Last change: nothing has moved since the last map."
    names = _names([])
    bits: list[str] = []
    added = change.get("added") or {}
    if added:
        top = sorted(added.items(), key=lambda kv: -kv[1])[:4]
        bits.append("+".join(f"{count} {names.get(key, key)}" for key, count in top))
        if len(added) > len(top):
            bits.append(f"and {len(added) - len(top)} more")
    if change.get("new_projects"):
        bits.append("new: " + ", ".join(change["new_projects"][:4]))
    if change.get("new_suggestions"):
        bits.append("possible projects: " + ", ".join(change["new_suggestions"][:3]))
    if change.get("gone_projects"):
        bits.append("gone: " + ", ".join(change["gone_projects"][:3]))
    if not bits:
        bits.append("the notes moved")
    return ("Last change: " + "; ".join(bits) + ".")


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_if_changed(path: Path, text: str) -> bool:
    """Write only when the content is different.

    A file that is rewritten with the same thing every hour is a file that
    changes every hour: the agent sees a "new" map when nothing happened,
    and git shows a diff where there is none.
    """
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    path.write_text(text, encoding="utf-8")
    return True


def _save_state(path: Path, new_state: dict[str, Any], change: dict[str, Any]) -> None:
    path.write_text(json.dumps({
        "fingerprint": new_state,
        "written_at": int(time.time()),
        "change": change,
    }, indent=2), encoding="utf-8")


def write_agent_map(*, filename: str = MAP_FILENAME, pointer: bool = True,
                    also_to: list[Path] | None = None) -> dict[str, Any]:
    """Write the map where the next agent will find it.

    This is the part that makes changing agents a non-event: the map is a
    file, not a memory. With ``pointer``, the agent's own instructions point
    at it, so an agent that follows its startup list reads the map before
    it answers anything.

    Nothing is written when nothing changed.
    """
    from . import library

    workspace = library.memory_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / filename
    state_path = workspace / STATE_FILENAME

    data = structure()
    text = export_markdown(data)
    new_state = fingerprint(data)
    old = _read_state(state_path)
    change = diff_maps(old.get("fingerprint") if old else None, new_state)

    written = _write_if_changed(path, text)
    if written or old is None:
        _save_state(state_path, new_state, change)

    out: dict[str, Any] = {
        "path": str(path),
        "bytes": len(text.encode("utf-8")),
        "changed": written,
        "change": change,
    }
    if pointer:
        out["pointer"] = write_agent_pointer(workspace, summary=summarize_change(change))
    # The same map where the notes live, so it travels with them.
    copies: list[str] = []
    for destination in (also_to or []):
        try:
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / filename
            if _write_if_changed(target, text):
                copies.append(str(target))
        except OSError as e:
            log.warning("could not write the map to %s: %s", destination, e)
    if copies:
        out["copies"] = copies
    return out


def write_agent_pointer(workspace: Path | None = None, *,
                        summary: str | None = None) -> dict[str, Any]:
    """Make the agent's own instructions point at the map.

    The block is marked, so it can be refreshed without touching the rest
    of the file: the user writes AGENTS.md, this only owns the two markers.
    It is written only when the summary changed — the user's own file must
    not be touched every hour.
    """
    if workspace is None:
        from . import library

        workspace = library.memory_workspace()
    path = workspace / "AGENTS.md"
    block = _pointer_block(summary or "> Last change: the map has just been drawn.")
    try:
        current = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        current = ""
    if POINTER_BEGIN in current and POINTER_END in current:
        start = current.index(POINTER_BEGIN)
        end = current.index(POINTER_END) + len(POINTER_END)
        updated = current[:start] + block.rstrip("\n") + current[end:]
    elif current:
        # Right after the startup heading, where an agent looks first.
        marker = "## Session Startup"
        if marker in current:
            at = current.index(marker) + len(marker)
            updated = current[:at] + "\n\n" + block + current[at:]
        else:
            updated = block + "\n" + current
    else:
        updated = block
    changed = updated != current
    if changed:
        path.write_text(updated, encoding="utf-8")
    return {"path": str(path), "bytes": len(updated.encode("utf-8")), "changed": changed}


def refresh_agent_map(*, filename: str = MAP_FILENAME,
                       also_to: list[Path] | None = None) -> dict[str, Any] | None:
    """Keep the map fresh, without touching the agent's instructions.

    The hourly re-index calls this. It only rewrites a map that is already
    there: the product does not start writing into the agent's workspace
    because it happened to run.
    """
    from . import library

    workspace = library.memory_workspace()
    path = workspace / filename
    if not path.exists():
        return None
    state_path = workspace / STATE_FILENAME
    data = structure()
    text = export_markdown(data)
    new_state = fingerprint(data)
    old = _read_state(state_path)
    change = diff_maps(old.get("fingerprint") if old else None, new_state)
    written = _write_if_changed(path, text)
    if written:
        _save_state(state_path, new_state, change)
    copies: list[str] = []
    for destination in (also_to or []):
        try:
            destination.mkdir(parents=True, exist_ok=True)
            if _write_if_changed(destination / filename, text):
                copies.append(str(destination / filename))
        except OSError as e:
            log.warning("could not write the map to %s: %s", destination, e)
    out = {"path": str(path), "bytes": len(text.encode("utf-8")), "changed": written,
           "change": change}
    if copies:
        out["copies"] = copies
    return out
