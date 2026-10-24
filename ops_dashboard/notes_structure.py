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
            # 1b. The registry itself resolves the name it was given: the
            # frontmatter says "polymarket-bot" and the project is called
            # "polymarketea", and the registry knows they are the same.
            for field in EXPLICIT_FIELDS:
                value = str(front.get(field) or "").strip()
                if not value:
                    continue
                resolved = projects.find_project(value)
                if resolved is not None and resolved.key == proj.key:
                    best = max(best or (0.0, ""), (1.2, f"frontmatter says {original}"))
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
                "tags": [t.strip().lstrip("#").lower()
                         for t in re.findall(r"[^\[\],\s]+", fm.get("tags", ""))],
                "title": fm.get("title") or path.stem,
                "links": _link_targets(text),
                "headings": [(i + 1, line.lstrip("#").strip())
                             for i, line in enumerate(text.splitlines())
                             if line.startswith("#")],
                "mtime": int(path.stat().st_mtime),
                "in_projects": rel.startswith("projects/"),
                # A project page sits directly in the projects folder; the
                # notes inside its own folder are its documentation.
                "is_project_page": rel.startswith("projects/")
                and rel.count("/") == 1,
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

    # A project page says so in its frontmatter and sits directly in the
    # projects folder. The notes inside a project's own folder are its
    # documentation — not projects — and they are what the "unlinked"
    # report is about.
    project_notes = [n for n in notes
                     if n["type"] == "project" and n["is_project_page"]]
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
        "repeats_itself": _notes_that_repeat_themselves(notes),
    }


def _notes_that_repeat_themselves(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The notes that contain a copy of themselves.

    It happens when a file is pasted into itself, or when a sync writes
    the same block twice. The cost is not the size: it is that every
    pending shows up twice and nobody can tell which one is the real one.

    A heading that appears twice is not enough — two sections may have
    the same name — so the copy has to be a long one: the same heading a
    hundred lines further down means the note holds two of itself.
    """
    out: list[dict[str, Any]] = []
    for note in notes:
        heads = note.get("headings") or []
        seen: dict[str, int] = {}
        for line, head in heads:
            seen.setdefault(head, line)
        for head, first in seen.items():
            again = [line for line, h in heads if h == head and line != first]
            if not again:
                continue
            span = again[0] - first
            if span < 100:
                continue
            out.append({
                "stem": note["stem"],
                "path": note.get("path") or "",
                "heading": head,
                "lines": span,
            })
            break
    return out


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


def _sections_of(text: str) -> list[dict[str, Any]]:
    """The sections of an index, with the notes each one lists.

    A heading opens a section; the entries under it are the notes it
    mentions. The heading hierarchy is kept, so a section knows the group
    it belongs to ("SaaS" under "🟢 Activos").
    """
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    group = ""
    for line in text.splitlines():
        heading = re.match(r"^(#{2,3})\s+(.+?)\s*$", line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if level == 2:
                group = title
            current = {"title": title, "group": group, "entries": []}
            sections.append(current)
            continue
        entry = re.match(r"^\s*[-*]\s+\[\[([^\]|]+)", line)
        if entry and current is not None:
            target = entry.group(1).strip().rstrip("\\").split("/")[-1].removesuffix(".md")
            if target:
                current["entries"].append(target)
    return sections


def propose_index_lines(*, roots: list[Path] | None = None) -> dict[str, Any]:
    """The lines that would put the missing projects back in the index.

    The index is the user's prose, so nothing is written. What the product
    offers is the list of lines that are missing, in the format the index
    already uses, and — where the notes give a clear hint — the section
    each one belongs in.

    The section is guessed from the tags of the projects the section
    already lists: if everything under "SaaS" is tagged ``saas``, a note
    tagged ``saas`` probably belongs there too. The guess is a guess, and
    it is marked as one.
    """
    roots = roots if roots is not None else _vault_roots()
    drift = index_drift(roots=roots)
    notes = _read_notes(roots)
    by_stem: dict[str, dict[str, Any]] = {}
    for note in notes:
        by_stem.setdefault(note["stem"], note)

    # The sections of the index, with the tags of the notes they list.
    #
    # Only the hub that lists the projects matters here: the other hubs
    # (the TODO, the map) have their own sections, and guessing from those
    # would put a project in "🔧 Mantenimiento" because that is where a
    # shared tag happened to appear.
    project_stems = {n["stem"] for n in notes if n["type"] == "project"
                     and n["is_project_page"]}
    sections: list[dict[str, Any]] = []
    best_hub: tuple[int, str, list[dict[str, Any]]] = (0, "", [])
    for root in roots:
        for path in root.rglob("*.md"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not _frontmatter(text).get("type", "").lower() == "hub":
                continue
            parsed = _sections_of(text)
            projects_listed = sum(
                1 for section in parsed
                for stem in section["entries"] if stem in project_stems)
            if projects_listed > best_hub[0]:
                best_hub = (projects_listed, path.name, parsed)
    for section in best_hub[2]:
        listed = [by_stem.get(stem) for stem in section["entries"]]
        listed = [n for n in listed if n]
        sections.append({
            "title": section["title"],
            "group": section["group"],
            "notes": listed,
            "count": len(section["entries"]),
            "hub": best_hub[1],
        })

    headings = [m.group(2).strip() for m in
                re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, re.M)] \
        if (text := _index_text(roots)) else []

    proposals: list[dict[str, Any]] = []
    for stem in drift["missing_from_index"]:
        note = by_stem.get(stem)
        if not note:
            continue
        wanted_tags = set(note["tags"]) - {"project"}
        best = None
        best_score = 0.0
        for section in sections:
            listed = section.get("notes") or []
            if not listed or not wanted_tags:
                continue
            # How many of the notes this section already lists carry the
            # tags of the candidate. A section of eleven notes has eleven
            # notes' worth of tags, so "the union contains it" would say
            # yes to everything.
            with_tag = sum(1 for n in listed if wanted_tags & set(n["tags"]))
            score = with_tag / len(listed)
            if score > best_score:
                best, best_score = section, score
        proposals.append({
            "stem": stem,
            "title": note["title"],
            "status": note["status"] or "(no status)",
            "tags": [t for t in note["tags"] if t != "project"],
            "line": f"- [[{stem}]] — {note['title']}",
            # The group the status decides is a fact; the subsection is a
            # hint, and a tag can mislead (an iOS app listed under the
            # trading bots pulls every other iOS note with it).
            "status_section": _status_group(note["status"]),
            "section": best["title"] if best and best_score >= 0.8 else "",
            "section_group": best["group"] if best and best_score >= 0.8 else "",
            "confidence": round(best_score, 2),
            # Where it goes by default: the group the status decides, which
            # is a fact. A subsection is only offered when the tags are
            # unmistakable.
            "default_section": _group_heading(_status_group(note["status"]),
                                              headings) or "",
        })

    # Grouped the way the index groups things: by status, then by section.
    by_status: dict[str, list[dict[str, Any]]] = {}
    for proposal in proposals:
        by_status.setdefault(proposal["status"], []).append(proposal)

    blocks: list[dict[str, Any]] = []
    for status, items in sorted(by_status.items(), key=lambda kv: -len(kv[1])):
        lines = [item["line"] for item in items]
        blocks.append({
            "status": status,
            "count": len(items),
            "lines": lines,
            "markdown": "\n".join(lines),
            "sections": sorted({i["section"] for i in items if i["section"]}),
        })

    # The ones that fit nowhere: the index has no section for their status,
    # and inventing one is a decision about the index, not about a note.
    unplaced = [p for p in proposals if not p["default_section"]]
    orphans = unlinked_notes(roots=roots)
    ideas = propose_ideas(roots=roots)

    return {
        "count": len(proposals),
        "proposals": proposals,
        "blocks": blocks,
        "unplaced": unplaced,
        "unplaced_section": NEW_SECTION_TITLE,
        "orphans": orphans,
        "ideas": ideas,
        "ideas_section": IDEAS_SECTION_TITLE,
        "sections": [{"title": s["title"], "group": s["group"], "notes": s["count"]}
                     for s in sections],
    }


# ── putting the index back in order ───────────────────────────────────────

# Where a status belongs, by the words the section uses. The words, not the
# emoji: another vault calls the same section "Active" or "Live".
STATUS_KEYWORDS = {
    "active": ("activo", "active", "🟢"),
    "paused": ("pausado", "paused", "🟡"),
    "archived": ("archiv", "muerto", "dead", "⚫"),
    "idea": ("idea",),
}

# Tags that say nothing about what a note is about. A daily note tagged
# "daily" is not an idea about daily notes.
NOT_A_THEME = {
    "daily", "log", "note", "notes", "inbox", "todo", "wip", "draft", "temp",
    "tmp", "journal", "meeting", "meta", "misc", "n/a", "none", "readme",
    "changelog", "index", "moc", "home",
}

# The section the product offers for the projects that fit nowhere else.
NEW_SECTION_TITLE = "📥 Sin clasificar"
# The section it adds to a project page for the notes it forgot to link.
ORPHAN_SECTION_TITLE = "📚 Documentación"
# The section it offers for the ideas that keep coming back.
IDEAS_SECTION_TITLE = "💡 Ideas"

COUNTS_RE = re.compile(
    r"\d+\s+proyectos\s*\(\s*\d+\s+notas activas\s*\+\s*\d+\s+ideas\s*\)")


def index_path(*, roots: list[Path] | None = None) -> Path | None:
    """The index of the projects: the hub that lists the most of them."""
    roots = roots if roots is not None else _vault_roots()
    notes = _read_notes(roots)
    project_stems = {n["stem"] for n in notes
                     if n["type"] == "project" and n["is_project_page"]}
    best: tuple[int, Path] | None = None
    for root in roots:
        for path in root.rglob("*.md"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _frontmatter(text).get("type", "").lower() != "hub":
                continue
            listed = sum(1 for stem in _link_targets(text) if stem in project_stems)
            if best is None or listed > best[0]:
                best = (listed, path)
    return best[1] if best and best[0] else None


def _is_heading(line: str, heading: str) -> bool:
    m = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
    return bool(m) and m.group(1).strip().lower() == heading.strip().lower()


def stems_in(lines: list[str]) -> list[str]:
    """The note names in a list of index lines."""
    out = []
    for line in lines:
        m = re.match(r"^-\s+\[\[([^\]|]+)", line)
        if m:
            out.append(m.group(1).strip())
    return out


def _section_of(text: str, heading: str) -> tuple[int, int] | None:
    """The line range of a section: where it starts and where it ends."""
    lines = text.splitlines()
    start = None
    level = 0
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not m:
            continue
        if start is None and heading.lower() in m.group(2).lower():
            start = i
            level = len(m.group(1))
            continue
        if start is not None and len(m.group(1)) <= level:
            return (start, i)
    if start is None:
        return None
    return (start, len(lines))


def _insert_into_section(lines: list[str], heading: str, entries: list[str]) -> tuple[list[str], bool]:
    """Put the entries in the section, where they cannot be mistaken for
    something else.

    A section with subsections (the active ones do) gets the entries right
    after its own heading: at the end of the group they would read as part
    of the last subsection, which is how a project ends up filed under
    "Clientes / agencia" for ever.
    """
    start = level = None
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not m:
            continue
        if start is None and heading.lower() in m.group(2).lower():
            start, level = i, len(m.group(1))
            continue
        if start is not None and len(m.group(1)) <= level:
            end = i
            break
    else:
        end = len(lines)
    if start is None:
        return lines, False
    has_subsections = any(
        re.match(r"^(#{2,6})\s+", ln) and len(re.match(r"^(#{1,6})", ln).group(1)) > level
        for ln in lines[start + 1:end])
    if has_subsections:
        # Right after the heading, before the first subsection.
        insert_at = start + 1
        while insert_at < end and not lines[insert_at].strip():
            insert_at += 1
        block = [""] + entries + [""]
        return lines[:insert_at] + block + lines[insert_at:], True
    # The last non-empty line of the section is where the list ends.
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    return lines[:insert_at] + entries + lines[insert_at:], True


def _index_text(roots: list[Path]) -> str:
    """The text of the projects index, or nothing."""
    path = index_path(roots=roots)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _group_heading(group: str, headings: list[str]) -> str:
    """The level-two heading that holds this group, by its words."""
    if not group:
        return ""
    words = group.lower().split(" / ")[0].split()
    for heading in headings:
        low = heading.lower()
        if any(w and w in low for w in words):
            return heading
    return ""


def _status_group(status: str) -> str:
    """The group a status belongs to, by what the status means.

    This is the one thing the product knows for sure: the note says what
    it is. The subsection inside the group is a different matter.
    """
    if status in ("", "active", "in-progress", "live", "ready", "shipped-to-review"):
        return "Activos"
    if status == "paused":
        return "Pausados"
    if status in ("archived", "done", "resolved"):
        return "Muertos / archivados"
    if status == "idea":
        return "Ideas"
    return ""


def _home_for(status: str, guessed: str, text: str) -> str:
    """The section a project of this status belongs to, in this index.

    The status decides (active goes with the active ones); the guess from
    the tags decides which subsection inside it. A section that is not
    there is not invented: the line is left for the user to place.
    """
    headings = [m.group(2).strip() for m in
                re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, re.M)]
    # The guess from the tags is more precise than the status: it knows
    # WHICH subsection, and not only which group.
    if guessed and any(h.strip() == guessed.strip() for h in headings):
        return guessed
    keywords = STATUS_KEYWORDS.get(status)
    if keywords:
        for heading in headings:
            low = heading.lower()
            if any(k in low for k in keywords):
                return heading
    return ""


def apply_index_lines(stems: list[str] | list[dict[str, Any]], *,
                      dry_run: bool = True,
                      roots: list[Path] | None = None) -> dict[str, Any]:
    """Put the chosen projects back in the index.

    The index is the user's prose, so the product only does three things
    and nothing else: it adds the lines it was told to add, at the end of
    the section they belong to, and it updates the counts in the header.
    Every word the user wrote stays.

    With ``dry_run`` (the default) nothing is touched: the result says
    what would change, so the user sees it before it happens.
    """
    roots = roots if roots is not None else _vault_roots()
    path = index_path(roots=roots)
    if path is None:
        return {"ok": False, "error": "no projects index found"}
    text = path.read_text(encoding="utf-8", errors="replace")
    original = text

    proposal = propose_index_lines(roots=roots)
    by_stem = {p["stem"]: p for p in proposal["proposals"]}
    # The caller may send just the names, or a name with the section the
    # user chose for it.
    chosen: dict[str, str] = {}
    for entry in stems:
        if isinstance(entry, dict):
            if entry.get("stem") in by_stem:
                chosen[entry["stem"]] = str(entry.get("section") or "")
        elif entry in by_stem:
            chosen[entry] = ""
    wanted = [s for s in chosen if s in by_stem]

    # Which section each one goes to.
    grouped: dict[str, list[str]] = {}
    no_home: list[str] = []
    for stem in wanted:
        item = by_stem[stem]
        heading = chosen[stem] or _home_for(item["status"], item["section"], text)
        if not heading:
            no_home.append(stem)
            continue
        grouped.setdefault(heading, []).append(item["line"])

    lines = text.splitlines()
    applied: list[str] = []
    for heading, entries in grouped.items():
        if not any(_is_heading(line, heading) for line in lines):
            # A section that does not exist yet: the product can offer one,
            # but only when the user asked for it by name.
            if heading not in (chosen.values() or []):
                no_home.extend(stems_in(entries))
                continue
            lines = lines + ["", f"## {heading}", ""] + entries + [""]
            applied.extend(entries)
            continue
        lines, ok = _insert_into_section(lines, heading, entries)
        if ok:
            applied.extend(entries)

    # The counts in the header, and only those.
    drift = index_drift(roots=roots)
    active = drift["by_status"].get("active", 0)
    ideas = drift["by_status"].get("idea", 0)
    total = drift["projects"]
    new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    new_text = COUNTS_RE.sub(f"{total} proyectos ({active} notas activas + {ideas} ideas)",
                              new_text, count=1)

    out: dict[str, Any] = {
        "ok": True,
        "path": str(path),
        "added": applied,
        "no_home": no_home,
        "counts": {"total": total, "active": active, "ideas": ideas},
        "changed": new_text != original,
    }
    if dry_run:
        out["preview"] = new_text
        return out

    if new_text == original:
        out["written"] = False
        return out
    # A copy before the change: the index is the user's prose. The first
    # copy is the one that matters, so it is never overwritten: a second
    # change must not replace the original with an intermediate state.
    backup = path.with_suffix(path.suffix + ".conductloop-backup")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
    path.write_text(new_text, encoding="utf-8")
    out["written"] = True
    out["backup"] = str(backup)
    _remember_index_change(path, applied, original)
    return out


def _remember_index_change(path: Path, added: list[str], before: str) -> None:
    """What the product added, so it can take it back."""
    from . import app_paths

    state_path = app_paths.path("library_index_state.json")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    state[str(path)] = {"added": added, "before": before,
                        "at": int(time.time())}
    try:
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def apply_orphan_links(project: str, stems: list[str], *,
                       dry_run: bool = True,
                       roots: list[Path] | None = None) -> dict[str, Any]:
    """Link the notes of a project that its page forgot.

    The notes are in the project's own folder and they keep naming it, but
    the page does not link them: in the vault, in the search, and in nobody's
    view when they open the project.

    The links go in a section of their own, at the end of the page. Nothing
    of the user's is touched, and there is a copy before the change.
    """
    roots = roots if roots is not None else _vault_roots()
    notes = _read_notes(roots)
    by_stem: dict[str, dict[str, Any]] = {}
    for note in notes:
        by_stem.setdefault(note["stem"], note)
    page = by_stem.get(project)
    if page is None:
        return {"ok": False, "error": f"no note called {project}"}
    path = Path(page["path"])
    text = path.read_text(encoding="utf-8", errors="replace")
    original = text

    lines = text.splitlines()
    entries = []
    for stem in stems:
        note = by_stem.get(stem)
        if not note:
            continue
        entries.append(f"- [[{stem}]] — {note['title']}")
    if not entries:
        return {"ok": False, "error": "nothing to link"}
    block = ["", f"## {ORPHAN_SECTION_TITLE}", ""] + entries + [""]
    new_text = "\n".join(lines + block) + ("\n" if text.endswith("\n") else "")

    out: dict[str, Any] = {
        "ok": True,
        "path": str(path),
        "added": entries,
        "changed": new_text != original,
    }
    if dry_run:
        out["preview"] = new_text
        return out
    if new_text == original:
        out["written"] = False
        return out
    backup = path.with_suffix(path.suffix + ".conductloop-backup")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
    path.write_text(new_text, encoding="utf-8")
    out["written"] = True
    out["backup"] = str(backup)
    return out


def undo_index_lines(*, roots: list[Path] | None = None) -> dict[str, Any]:
    """Take back the lines the product added."""
    from . import app_paths

    state_path = app_paths.path("library_index_state.json")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "error": "nothing to undo"}
    if not isinstance(state, dict) or not state:
        return {"ok": False, "error": "nothing to undo"}
    path = Path(list(state.keys())[0])
    entry = state[str(path)]
    if not path.exists():
        return {"ok": False, "error": "the index is not there any more"}
    text = path.read_text(encoding="utf-8", errors="replace")
    removed = []
    for line in entry.get("added") or []:
        if line in text:
            text = text.replace(line + "\n", "", 1)
            removed.append(line)
    path.write_text(text, encoding="utf-8")
    state.pop(str(path), None)
    try:
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass
    return {"ok": True, "path": str(path), "removed": removed}


def propose_ideas(*, roots: list[Path] | None = None,
                  min_notes: int = 5) -> list[dict[str, Any]]:
    """The ideas that keep coming back.

    The loose notes are not noise: they are the ideas that have not found a
    project yet. A theme with notes in it is an idea, and the more notes it
    has the more it is one.

    The theme is a tag, because a tag is what the notes themselves say.
    """
    roots = roots if roots is not None else _vault_roots()
    notes = _read_notes(roots)
    project_stems = {n["stem"] for n in notes
                     if n["type"] == "project" and n["is_project_page"]}
    # The notes that are not a project page of their own.
    loose = [n for n in notes if n["stem"] not in project_stems]

    groups: dict[str, list[dict[str, Any]]] = {}
    for note in loose:
        for tag in note["tags"]:
            if tag in ("project", "idea", "hub") or tag in NOT_A_THEME:
                continue
            groups.setdefault(tag, []).append(note)

    out: list[dict[str, Any]] = []
    for tag, items in groups.items():
        if len(items) < min_notes:
            continue
        items.sort(key=lambda n: -n["mtime"])
        out.append({
            "theme": tag,
            "title": tag.replace("-", " ").replace("_", " ").capitalize(),
            "notes": len(items),
            "lines": [f"- [[{n['stem']}]] — {n['title']}" for n in items[:12]],
            "recent": [{"stem": n["stem"], "title": n["title"], "path": n["path"]}
                       for n in items[:5]],
        })
    out.sort(key=lambda item: -item["notes"])
    return out


PROPOSAL_FILENAME = "PROPOSAL.md"


def write_proposal(*, filename: str = PROPOSAL_FILENAME) -> dict[str, Any] | None:
    """Write what the product would do, for the user to find in the morning.

    The nightly pass draws the map and notices what fell out of the index;
    this writes it down where the next agent — and the user — will see it.
    It proposes; it does not touch the user's files.
    """
    from . import library

    workspace = library.memory_workspace()
    path = workspace / filename
    proposal = propose_index_lines()
    drift = proposal and index_drift()
    ideas = proposal.get("ideas") or []
    orphans = proposal.get("orphans") or []
    unplaced = proposal.get("unplaced") or []

    pending = len(proposal.get("proposals") or []) + len(unplaced) + len(orphans)
    if not pending:
        # Nothing to propose: the file is removed rather than left saying
        # "nothing to do" for ever.
        if path.exists():
            path.unlink()
        return None

    lines = ["# What the product would do", "",
             "Drawn by the nightly pass. Nothing has been changed: these are the",
             "lines that would go where they belong.", ""]
    if proposal.get("proposals"):
        lines.append(f"## Projects that are in no index ({len(proposal['proposals'])})")
        lines.append("")
        for block in proposal["blocks"]:
            lines.append(f"### {block['status']}")
            lines.extend(block["lines"])
            lines.append("")
    if unplaced:
        lines.append(f"## Projects that fit in no section ({len(unplaced)})")
        lines.append("")
        for item in unplaced:
            lines.append(f"- {item['line']}")
        lines.append("")
        lines.append(f"A section for them would be called “{proposal['unplaced_section']}”.")
        lines.append("")
    if orphans:
        lines.append(f"## Notes a project page does not link ({len(orphans)})")
        lines.append("")
        for row in orphans:
            lines.append(f"- **{row['title']}** — {row['count']} note(s): "
                         + ", ".join(f"[[{s}]]" for s in row["orphans"]))
        lines.append("")
    if ideas:
        lines.append(f"## Ideas that keep coming back ({len(ideas)})")
        lines.append("")
        for idea in ideas[:10]:
            sample = ", ".join(f"[[{n['stem']}]]" for n in idea["recent"][:3])
            lines.append(f"- **{idea['title']}** — {idea['notes']} notes · {sample}")
        if len(ideas) > 10:
            lines.append(f"- … and {len(ideas) - 10} more")
        lines.append("")
    if drift:
        broken = drift.get("links_to_nothing") or []
        if broken:
            lines.append(f"## Links that point to nothing ({len(broken)})")
            lines.append("")
            for stem in broken:
                lines.append(f"- {stem}")
            lines.append("")
    lines.append(f"_Drawn {time.strftime('%Y-%m-%d %H:%M')} · "
                 f"{drift['projects'] if drift else 0} projects in the vault_")

    workspace.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    _write_if_changed(path, text)
    return {"path": str(path), "bytes": len(text.encode("utf-8")), "pending": pending}


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


def _pointer_block(summary: str, pages: str = "") -> str:
    extra = ""
    if pages:
        extra = (">\n"
                 f"> The same map, one page per project, is in your vault at\n"
                 f"> `{pages}`.\n"
                 "> Read that page when you need one project: it is a page, not 39 KB.\n")
    return f"""{POINTER_BEGIN}
> **The map of the work** — `{MAP_FILENAME}` in this folder. Which project each
> note belongs to, which ideas belong to nobody yet, and the projects your
> notes know about but the registry does not. Read it before answering
> anything about a project: it is the same for every agent, which is the
> point of writing it down.
>
> {summary}
{extra}{POINTER_END}
"""


def _pages_index() -> str:
    """Where the pages of the map are, when there is a vault to write them in.

    Empty when they are not there: the pointer must never send an agent to
    a file that does not exist.
    """
    from . import library
    from .vault_pages import INDEX_NAME, PAGES_DIRNAME

    destinations = library.map_export_destinations()
    if not destinations:
        return ""
    path = destinations[0] / PAGES_DIRNAME / INDEX_NAME
    return str(path) if path.exists() else ""


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
    # And as pages, one per project, linked: the shape a vault understands.
    pages_written = 0
    if also_to:
        try:
            from . import vault_pages

            out["pages"] = vault_pages.export_pages(destinations=list(also_to), data=data)
            pages_written = int(out["pages"].get("pages") or 0)
        except Exception as e:
            # The pages are a view of the map; the map itself is already written.
            log.warning("could not write the pages of the map: %s", e)
    # The pointer goes last: it must never point at pages that are not there.
    if pointer:
        out["pointer"] = write_agent_pointer(
            workspace, summary=summarize_change(change),
            proposal=proposal_path(workspace),
            pages=_pages_index() if pages_written else "")
    return out


def proposal_path(workspace: Path | None = None) -> str:
    """The proposal file, when there is one waiting."""
    if workspace is None:
        from . import library

        workspace = library.memory_workspace()
    path = workspace / PROPOSAL_FILENAME
    return str(path) if path.exists() else ""


def write_agent_pointer(workspace: Path | None = None, *,
                        summary: str | None = None,
                        proposal: str = "",
                        pages: str | None = None) -> dict[str, Any]:
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
    block = _pointer_block(summary or "> Last change: the map has just been drawn.",
                            pages=_pages_index() if pages is None else pages)
    if proposal:
        block = block.replace(
            f"{POINTER_END}",
            f">\n> There are things waiting for you in `{PROPOSAL_FILENAME}`.\n{POINTER_END}")
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
    if also_to:
        try:
            from . import vault_pages

            out["pages"] = vault_pages.export_pages(destinations=list(also_to), data=data)
        except Exception as e:
            log.warning("could not write the pages of the map: %s", e)
    return out
