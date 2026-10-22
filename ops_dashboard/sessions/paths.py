"""Path-claim rules (design doc §6, decision D3). Pure functions, no I/O.

Patterns are repo-relative globs (``fnmatch`` syntax). A literal pattern (no
glob characters) means "this file or this directory and everything under it".

Overlap is decided conservatively from the patterns alone: every path a
pattern can match starts with its *literal prefix* (the text before the first
glob character), so two patterns whose literal prefixes diverge can never
match the same file; if one prefix extends the other they are treated as
overlapping. Literal-vs-literal comparisons respect path boundaries
(``src/a`` does not overlap ``src/ab.py``). A precise check against the real
file list (``git ls-files``) is ``precise_overlap`` below, used by the code
tools (PR 5); writes are then re-checked per concrete path, so the precise
check can never let two sessions write the same file.
"""
from __future__ import annotations

import fnmatch
import itertools
import posixpath
from collections.abc import Iterable

# D3: files that are always claimed exclusively — one task at a time.
HOT_BASENAMES = ("server.py", "index.html", "db.py")
LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
             "bun.lock", "bun.lockb", "poetry.lock", "uv.lock", "Pipfile.lock", "pdm.lock",
             "requirements.lock", "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum",
             "mix.lock", "flake.lock")
EXCLUSIVE_BASENAMES = HOT_BASENAMES + LOCKFILES
_GLOB_CHARS = "*?["


class PathConflictError(RuntimeError):
    """A requested claim overlaps an active claim it may not share. Nothing was
    granted. ``conflicts`` lists dicts with the requested pattern and the
    holder's claim (pattern, session_id, task_id, mode)."""

    def __init__(self, conflicts: list[dict]):
        first = conflicts[0]
        super().__init__(
            f"'{first['pattern']}' overlaps '{first['held_pattern']}' claimed "
            f"({first['held_mode']}) by session #{first['held_session_id']}")
        self.conflicts = conflicts


def normalize(pattern: str) -> str:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("empty path pattern")
    p = pattern.strip().replace("\\", "/")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        raise ValueError(f"path pattern must be repo-relative: {pattern!r}")
    parts = [s for s in p.split("/") if s not in ("", ".")]
    if any(s == ".." for s in parts):
        raise ValueError(f"path pattern may not contain '..': {pattern!r}")
    return "/".join(parts) or "**"


def _is_glob(p: str) -> bool:
    return any(c in p for c in _GLOB_CHARS)


def literal_prefix(p: str) -> str:
    idx = min((p.index(c) for c in _GLOB_CHARS if c in p), default=len(p))
    return p[:idx]


def overlaps(a: str, b: str) -> bool:
    a, b = normalize(a), normalize(b)
    if a == b:
        return True
    la, lb = literal_prefix(a), literal_prefix(b)
    short, long_, short_is_glob = (la, lb, _is_glob(a)) if len(la) <= len(lb) else (lb, la, _is_glob(b))
    if not long_.startswith(short):
        return False  # diverging literal prefixes: no path can match both
    if short_is_glob or short == "":
        return True
    # Short side is a literal file/dir: overlap only at a path boundary.
    return len(long_) == len(short) or long_[len(short)] == "/"


def is_exclusive(pattern: str) -> bool:
    """True when ``pattern`` can match a hot file / lockfile by name."""
    p = normalize(pattern)
    base = posixpath.basename(p)
    if not _is_glob(base):
        return base in EXCLUSIVE_BASENAMES
    return any(fnmatch.fnmatchcase(name, base) for name in EXCLUSIVE_BASENAMES)


def covers(pattern: str, path: str) -> bool:
    """True when a claim on ``pattern`` authorises writing ``path``."""
    p, f = normalize(pattern), normalize(path)
    if _is_glob(f):
        raise ValueError("a write target must be a concrete path")
    if not _is_glob(p):
        return f == p or f.startswith(p + "/")
    return fnmatch.fnmatchcase(f, p)


def precise_overlap(a: str, b: str, files: Iterable[str]) -> bool:
    """``overlaps`` refined with the worktree's real file list: two patterns
    clash only if some existing file (or one of them, when literal) matches
    both. Fewer false positives than ``overlaps``; glob-vs-glob clashes on
    files that don't exist yet are caught at write time (``covers`` per path)."""
    if not overlaps(a, b):
        return False
    a, b = normalize(a), normalize(b)
    if not _is_glob(a) and not _is_glob(b):
        return True  # literal vs literal: overlaps() is already exact
    candidates = [x for x in (a, b) if not _is_glob(x)]
    for f in itertools.chain(candidates, files):
        if covers(a, f) and covers(b, f):
            return True
    return False
