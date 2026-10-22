"""Project registry + rollups.

Each project has aliases (for free-text matching) and a list of paths / URL names
that belong to it. The rollup command gathers everything we know about a project:
documents mentioning it, recent commits, live URL health, etc.
"""
from __future__ import annotations

import json as _json
import logging
import os as _os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .indexers import live_urls

_log = logging.getLogger("conductloop.projects")


@dataclass
class Project:
    key: str
    name: str
    aliases: list[str]
    repo_paths: list[str] = field(default_factory=list)
    obsidian_globs: list[str] = field(default_factory=list)  # path globs under vault
    url_names: list[str] = field(default_factory=list)        # names in live_urls table
    color: str = "cyan"
    parent: str | None = None  # key of the parent project (e.g. 'ev-trading-labs')
    github_full: str | None = None  # 'owner/repo' for GitHub sync (optional)
    github_repo: str | None = None  # 'org/name' for GitHub sync (optional)


def children_of(parent_key: str) -> list[Project]:
    """Return all projects whose parent == parent_key."""
    return [p for p in PROJECTS if p.parent == parent_key]


def is_parent(project: Project) -> bool:
    """A project is a 'parent' if it has at least one child."""
    return any(p.parent == project.key for p in PROJECTS)


# Project registry. Identity-neutral: ships EMPTY. A fresh install has no
# projects until the user defines their own, so no personal data is baked into
# the source. Load order:
#   1. OPS_PROJECTS_FILE  — path to a JSON list of project dicts
#   2. projects.local.json next to the repo root (gitignored)
# Each dict: {key, name, aliases[], repo_paths[], url_names[], color, parent,
#             github_repo, obsidian_globs[]}
def _project_from_dict(d: dict) -> Project:
    return Project(
        key=d["key"],
        name=d.get("name", d["key"]),
        aliases=d.get("aliases", []),
        repo_paths=[str(Path(p).expanduser()) for p in d.get("repo_paths", [])],
        obsidian_globs=d.get("obsidian_globs", []),
        url_names=d.get("url_names", []),
        color=d.get("color", "cyan"),
        parent=d.get("parent"),
        github_full=d.get("github_full"),
        github_repo=d.get("github_repo"),
    )


_DEMO_PROJECTS = [
    {"key": "mosaic", "name": "Mosaic", "aliases": ["mosaic"], "color": "blue"},
    {"key": "verdant", "name": "Verdant (client)", "aliases": ["verdant"], "color": "green"},
]


def _load_projects() -> list[Project]:
    # Demo mode never reads the local (personal) registry — it would leak real
    # project names + home paths. Use fictional projects matching the demo data.
    if _os.environ.get("CONDUCTLOOP_DEMO") == "1":
        return [_project_from_dict(d) for d in _DEMO_PROJECTS]
    candidates: list[Path] = []
    env = _os.environ.get("OPS_PROJECTS_FILE")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path(__file__).resolve().parent.parent / "projects.local.json")
    for path in candidates:
        try:
            if path.exists():
                data = _json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [_project_from_dict(d) for d in data]
        except Exception as e:
            _log.warning("failed to load %s: %s", path, e)
    return []


PROJECTS: list[Project] = _load_projects()


def _slug(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower())


def find_project(query: str) -> Project | None:
    q = _slug(query)
    for p in PROJECTS:
        if p.key == q:
            return p
        for a in p.aliases:
            if a == q:
                return p
    # fuzzy: substring match
    for p in PROJECTS:
        for a in p.aliases:
            if a in q or q in a:
                return p
    return None


def _matching_docs(project: Project, limit: int = 10) -> list[dict]:
    """Find documents whose path or content mentions any of the project's aliases."""
    patterns = project.aliases + [project.name.lower(), project.key]
    # Build a big OR LIKE; SQLite can't bind OR-of-many LIKEs easily, so we
    # run one query per alias and dedupe by id.
    seen: dict[int, dict] = {}
    with db.db() as conn:
        for alias in patterns:
            like = f"%{alias.lower()}%"
            for r in conn.execute(
                """
                SELECT id, source, path, title, mtime
                FROM documents
                WHERE lower(path) LIKE ? OR lower(IFNULL(title, '')) LIKE ?
                ORDER BY mtime DESC LIMIT ?
                """,
                (like, like, limit * 2),
            ).fetchall():
                if r["id"] not in seen:
                    seen[r["id"]] = dict(r)
                if len(seen) >= limit:
                    break
            if len(seen) >= limit:
                break
    return list(seen.values())[:limit]


def _repo_activity(project: Project) -> list[dict]:
    """Return mtime + title for each repo path on disk.

    Falls back to looking up the name under ~ and $OPS_WORKSPACE.
    """
    from .indexers.repos import _resolve as _repo_resolve
    rows: list[dict] = []
    seen: set[str] = set()
    for rp in project.repo_paths:
        p = Path(rp)
        if p.exists() and str(p) not in seen:
            stat = p.stat()
            rows.append({"path": str(p), "name": p.name, "mtime": int(stat.st_mtime)})
            seen.add(str(p))
    # Also try resolving by basename in case the configured path is wrong.
    if not rows:
        for alias in project.aliases + [project.key]:
            p = _repo_resolve(alias)
            if p and str(p) not in seen:
                stat = p.stat()
                rows.append({"path": str(p), "name": p.name, "mtime": int(stat.st_mtime)})
                seen.add(str(p))
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def _url_health(project: Project) -> list[dict]:
    """Return live URL rows that belong to this project.

    Match by `name` (configured url_names) OR by the `project` field in the
    live_urls table (e.g. 'evtl-tools' → its own live URLs).
    """
    rows = live_urls.latest()
    if not project.url_names and not project.key:
        return []
    name_set = set(project.url_names or [])
    return [r for r in rows if r["name"] in name_set or r.get("project") == project.key]


def rollup(query: str) -> dict | None:
    project = find_project(query)
    if not project:
        return None
    # If this project is a parent (has children), aggregate from children too.
    child_projects = children_of(project.key) if is_parent(project) else []
    # URLs: own + children
    own_urls = _url_health(project)
    extra_urls = []
    seen_names = {u["name"] for u in own_urls}
    for child in child_projects:
        for u in _url_health(child):
            if u["name"] not in seen_names:
                extra_urls.append(u)
                seen_names.add(u["name"])
    all_urls = own_urls + extra_urls
    all_ok = all(u["ok"] for u in all_urls) if all_urls else None
    # Repos: own + children
    own_repos = _repo_activity(project)
    extra_repos = []
    seen_paths = {r["path"] for r in own_repos}
    for child in child_projects:
        for r in _repo_activity(child):
            if r["path"] not in seen_paths:
                extra_repos.append(r)
                seen_paths.add(r["path"])
    all_repos = own_repos + extra_repos
    # Docs: own + children (with aliases from each)
    own_docs = _matching_docs(project)
    extra_docs = []
    seen_doc_ids = {d["id"] for d in own_docs}
    for child in child_projects:
        for d in _matching_docs(child):
            if d["id"] not in seen_doc_ids:
                extra_docs.append(d)
                seen_doc_ids.add(d["id"])
    all_docs = own_docs + extra_docs
    # Last activity
    last_activity = max(
        [r["mtime"] for r in all_repos] +
        [u["checked_at"] for u in all_urls if u.get("checked_at")] +
        [d["mtime"] for d in all_docs if d.get("mtime")],
        default=0,
    )
    return {
        "project": {
            "key": project.key,
            "name": project.name,
            "aliases": project.aliases,
            "color": project.color,
            "parent": project.parent,
            "is_parent": bool(child_projects),
            "children": [c.key for c in child_projects],
        },
        "docs": all_docs,
        "repos": all_repos,
        "urls": all_urls,
        "all_urls_ok": all_ok,
        "last_activity": last_activity,
    }


def all_projects_summary() -> list[dict]:
    """Compact one-row-per-project view for dashboard cards."""
    url_by_name = {r["name"]: r for r in live_urls.latest()}
    docs_by_source = {}
    repos_by_path = {}
    with db.db() as conn:
        for r in conn.execute("SELECT source, COUNT(*) AS c, MAX(mtime) AS m FROM documents GROUP BY source"):
            docs_by_source[r["source"]] = {"count": r["c"], "latest": r["m"]}
        for r in conn.execute("SELECT path, title, mtime FROM documents WHERE source = 'repo'"):
            repos_by_path[r["path"]] = {"title": r["title"], "mtime": r["mtime"]}

    rows: list[dict] = []
    for p in PROJECTS:
        repo_rows = _repo_activity(p)
        url_rows = []
        if p.url_names:
            url_rows = [url_by_name.get(n) for n in p.url_names if url_by_name.get(n)]
        # Count obsidian mentions
        mention_count = 0
        last_mention_ts = 0
        for alias in p.aliases:
            like = f"%{alias.lower()}%"
            with db.db() as conn:
                c = conn.execute(
                    "SELECT COUNT(*) AS c, MAX(mtime) AS m FROM documents WHERE lower(path) LIKE ? OR lower(IFNULL(title, '')) LIKE ?",
                    (like, like),
                ).fetchone()
                mention_count += c["c"] or 0
                if c["m"] and c["m"] > last_mention_ts:
                    last_mention_ts = c["m"]

        last_activity = max(
            [r["mtime"] for r in repo_rows] +
            [r["checked_at"] for r in url_rows if r.get("checked_at")] +
            [last_mention_ts],
            default=0,
        )
        all_ok = all(r["ok"] for r in url_rows) if url_rows else None
        url_ok_count = sum(1 for r in url_rows if r["ok"])
        url_total = len(url_rows)
        rows.append({
            "key": p.key,
            "name": p.name,
            "color": p.color,
            "repos": repo_rows,
            "urls": url_rows,
            "all_urls_ok": all_ok,
            "url_ok": url_ok_count,
            "url_total": url_total,
            "mention_count": mention_count,
            "last_activity": last_activity,
        })
    rows.sort(key=lambda r: r["last_activity"], reverse=True)
    return rows
