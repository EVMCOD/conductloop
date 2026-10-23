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
            "recent": notes[:5], "repos": [], "known": False,
        })

    ideas: dict[str, list[dict[str, Any]]] = {}
    for note in loose:
        ideas.setdefault(note["area"], []).append(note)

    return {
        "projects": sorted(out_projects, key=lambda p: (-p["notes"], p["name"].lower())),
        "ideas": [
            {"area": area, "notes": notes[:50], "count": len(notes)}
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
    """The map as markdown: what an agent can read in one go."""
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
    suggestions_out = data.get("suggestions") or []
    if suggestions_out:
        lines.append("## Possible projects (not registered yet)")
        lines.append("")
        for suggestion in suggestions_out:
            lines.append(f"- **{suggestion['name']}** — {suggestion['why']}")
        lines.append("")
    totals = data["totals"]
    lines.append(f"_{totals['documents']} notes · {totals['attributed']} in a project "
                 f"· {totals['loose']} loose · generated {time.strftime('%Y-%m-%d %H:%M')}_")
    return "\n".join(lines) + "\n"


MAP_FILENAME = "PROJECTS.md"
POINTER_BEGIN = "<!-- conductloop:map -->"
POINTER_END = "<!-- /conductloop:map -->"

POINTER_BLOCK = f"""{POINTER_BEGIN}
> **The map of the work** — `MAP_FILENAME` in this folder. Which project each
> note belongs to, which ideas belong to nobody yet, and the projects your
> notes know about but the registry does not. Read it before answering
> anything about a project: it is the same for every agent, which is the
> point of writing it down.
{POINTER_END}
"""


def write_agent_map(*, filename: str = MAP_FILENAME, pointer: bool = True) -> dict[str, Any]:
    """Write the map where the next agent will find it.

    This is the part that makes changing agents a non-event: the map is a
    file, not a memory. With ``pointer``, the agent's own instructions point
    at it, so an agent that follows its startup list reads the map before
    it answers anything.
    """
    from . import library

    workspace = library.memory_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / filename
    text = export_markdown()
    path.write_text(text, encoding="utf-8")
    out = {"path": str(path), "bytes": len(text.encode("utf-8"))}
    if pointer:
        out["pointer"] = write_agent_pointer(workspace)
    return out


def write_agent_pointer(workspace: Path | None = None) -> dict[str, Any]:
    """Make the agent's own instructions point at the map.

    The block is marked, so it can be refreshed without touching the rest
    of the file: the user writes AGENTS.md, this only owns the two markers.
    """
    if workspace is None:
        from . import library

        workspace = library.memory_workspace()
    path = workspace / "AGENTS.md"
    block = POINTER_BLOCK.replace("MAP_FILENAME", MAP_FILENAME)
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
    path.write_text(updated, encoding="utf-8")
    return {"path": str(path), "bytes": len(updated.encode("utf-8"))}


def refresh_agent_map(*, filename: str = MAP_FILENAME) -> dict[str, Any] | None:
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
    text = export_markdown()
    path.write_text(text, encoding="utf-8")
    return {"path": str(path), "bytes": len(text.encode("utf-8"))}
