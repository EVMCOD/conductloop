"""Code repositories indexer.

For each known repo on disk, indexes:
  - README / INDEX
  - top-level structure
  - last N commits
  - branches
  - working-tree status

A repo can live under either ~/ or $OPS_WORKSPACE.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .. import db

HOME = Path.home()
WORKSPACE = Path(os.environ.get("OPS_WORKSPACE", str(HOME / ".ops-workspace"))).expanduser()
MAX_COMMITS = 50

# Identity-neutral: no repos hardcoded. The set to index is derived (in order):
#   1. OPS_REPOS env (space/comma-separated repo basenames)
#   2. basenames of repo_paths in the project registry (projects.local.json)
KNOWN_REPOS: list[str] = []


def _known_repos() -> list[str]:
    if KNOWN_REPOS:
        return KNOWN_REPOS
    env = os.environ.get("OPS_REPOS")
    if env:
        return [r.strip() for r in env.replace(",", " ").split() if r.strip()]
    try:
        from .. import projects as _proj
        seen: set[str] = set()
        out: list[str] = []
        for p in _proj.PROJECTS:
            for rp in p.repo_paths:
                name = Path(rp).name
                if name and name not in seen:
                    seen.add(name)
                    out.append(name)
        return out
    except Exception:
        return []


def _git(cmd: list[str], cwd: Path) -> str:
    try:
        out = subprocess.run(
            ["git", *cmd],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _registered_paths() -> dict[str, Path]:
    """basename -> absolute path, from the project registry.

    The registry knows where the repos really live; resolving by basename
    alone picked the wrong folder (or a hidden one) under $HOME.
    """
    out: dict[str, Path] = {}
    try:
        from .. import projects as _proj
        for p in _proj.PROJECTS:
            for rp in p.repo_paths or []:
                path = Path(rp).expanduser()
                if path.name:
                    out.setdefault(path.name, path)
    except Exception:
        pass
    return out


def _resolve(name: str) -> Path | None:
    """Find the repo by name.

    1. The project registry wins: it knows the absolute path.
    2. Otherwise a basename match under $HOME / $OPS_WORKSPACE, but only for a
       real repository: never a hidden directory (.ssh, .aws, .config …) and
       never a folder without .git. Indexing those would pull secrets into
       the RAG. 2026-10-23.
    """
    registered = _registered_paths()
    if name in registered:
        path = registered[name]
        return path if path.exists() else None
    for base in (HOME, WORKSPACE):
        p = base / name
        if not p.exists() or not p.is_dir():
            continue
        if p.name.startswith("."):
            continue  # hidden directory: never a repo
        if not (p / ".git").exists():
            continue  # not a git checkout
        return p
    return None


def _index_repo(path: Path) -> dict:
    counters = {"indexed": 0, "skipped": 0, "errors": 0}
    if not path.exists():
        return counters

    name = path.name
    parts: list[str] = [f"# Repo: {name}", f"path: {path}"]

    # README
    for candidate in ("README.md", "README.MD", "readme.md", "INDEX.md", "index.md"):
        rp = path / candidate
        if rp.exists():
            try:
                parts.append(f"\n## {candidate}\n")
                parts.append(rp.read_text(encoding="utf-8", errors="replace")[:8000])
            except Exception:
                pass
            break

    # Tree (top levels)
    try:
        parts.append("\n## File tree (top levels)")
        for entry in sorted(path.iterdir()):
            if entry.name.startswith("."):
                continue
            marker = "/" if entry.is_dir() else ""
            parts.append(f"- {entry.name}{marker}")
    except Exception:
        pass

    # Git log
    if (path / ".git").exists():
        log = _git(["log", f"--max-count={MAX_COMMITS}", "--pretty=format:%h %ad %s", "--date=short"], path)
        if log:
            parts.append("\n## Recent commits")
            parts.append("```")
            parts.append(log)
            parts.append("```")

        branches = _git(["branch", "--all", "--no-color"], path)
        if branches:
            parts.append("\n## Branches")
            parts.append("```")
            parts.append(branches[:1500])
            parts.append("```")

        status = _git(["status", "--porcelain"], path)
        parts.append(f"\n## Working tree status\n{status or '(clean)'}")

    content = "\n".join(parts)
    stat = path.stat()
    metadata = {
        "repo": name,
        "has_git": (path / ".git").exists(),
    }

    was_upserted = db.upsert_document(
        source="repo",
        path=str(path),
        title=name,
        content=content,
        metadata=metadata,
        mtime=int(stat.st_mtime),
        size=stat.st_size,
    )
    if was_upserted:
        counters["indexed"] += 1
    else:
        counters["skipped"] += 1
    return counters


def run(repos: list[str] | None = None) -> dict:
    repo_names = repos or _known_repos()
    run_id = db.start_index_run("repo")
    total = {"indexed": 0, "skipped": 0, "errors": 0}
    found = 0
    for name in repo_names:
        path = _resolve(name)
        if path is None:
            continue
        found += 1
        c = _index_repo(path)
        for k, v in c.items():
            total[k] += v
    db.end_run(run_id, **total, notes=f"repos_found={found} of {len(repo_names)} (home={HOME}, workspace={WORKSPACE})")
    print(f"repo: done. indexed={total['indexed']} skipped={total['skipped']} errors={total['errors']} (found {found}/{len(repo_names)})")
    return total
