"""Git worktrees for multi-agent sessions (multi-agent PR 4).

One worktree per session, OUTSIDE the user's repo, at
``<root>/<repo-slug>/<session-id>`` (root defaults to ``<data dir>/worktrees``,
override with ``CONDUCTLOOP_WORKTREES_DIR``), on its own branch
``cl/<role>/<session-id>`` created from ``base_ref``.

Hard rules enforced here (design doc §3/§6):

* never push / fetch / talk to a remote: git runs through an allow-list of
  local subcommands, with ``GIT_TERMINAL_PROMPT=0`` and no credential helper;
* never touch ``main``/``master`` or the base branch, never check out a branch
  that isn't ``cl/…``, never operate on the main checkout or on a worktree
  that isn't ours (under the root, registered in ``git worktree list``);
* repo hooks never run (``core.hooksPath`` → empty dir), API-key env vars are
  dropped from git's environment;
* a worktree is only removed when it is clean AND its branch has no commits
  beyond the base — work is never deleted.

``dry_merge`` simulates integration with ``git merge-tree --write-tree``
(git >= 2.38) without touching any checkout.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

PROTECTED_BRANCHES = ("main", "master")
BRANCH_PREFIX = "cl/"
WORKTREES_ENV = "CONDUCTLOOP_WORKTREES_DIR"
MIN_MERGE_TREE_GIT = (2, 38)

# Local-only subcommands this module may run. Anything else (push, fetch,
# pull, remote, clone, submodule, config, gc…) is refused before exec.
_ALLOWED_GIT = frozenset({"rev-parse", "worktree", "status", "rev-list", "diff",
                          "merge-tree", "branch", "check-ref-format", "version",
                          "show-ref", "symbolic-ref", "ls-files", "grep",
                          "update-ref"})  # only prune(): compare-and-delete of a cl/ branch
# Only commit_paths() may stage/commit, and only on a session's cl/ branch.
_COMMIT_GIT = frozenset({"add", "commit"})
# Env vars that could redirect git to another repo/index or leak secrets.
_DROP_ENV = re.compile(r"^(GIT_(DIR|WORK_TREE|INDEX_FILE|OBJECT_DIRECTORY|"
                       r"ALTERNATE_OBJECT_DIRECTORIES|COMMON_DIR|NAMESPACE|CEILING_DIRECTORIES|"
                       r"CONFIG.*|ASKPASS|SSH.*|EXEC_PATH|PROXY_COMMAND)|SSH_ASKPASS|"
                       r".*_API_KEY|.*_TOKEN|.*_SECRET.*)$")
_EMPTY_HOOKS = Path(tempfile.gettempdir()) / "conductloop-no-hooks"


class WorktreeError(RuntimeError):
    """A refused or failed worktree operation (message is safe to show)."""


@dataclass
class WorktreeInfo:
    path: str
    branch: str
    base_ref: str
    head: str = ""
    dirty: bool = False
    changed: list[str] = field(default_factory=list)   # uncommitted (porcelain paths)
    ahead: int = 0                                       # commits on branch beyond base
    files_vs_base: list[str] = field(default_factory=list)


@dataclass
class PruneResult:
    removed: bool
    reason: str
    info: WorktreeInfo | None = None


@dataclass
class DryMergeResult:
    clean: bool
    conflicts: list[str]
    tree: str | None = None


# ── git plumbing ────────────────────────────────────────────────────────────

def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _DROP_ENV.match(k)}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    return env


def _git(cwd: str | Path, *args: str, check: bool = True, timeout: int = 60,
         config: tuple[str, ...] = (),
         _commit: bool = False) -> subprocess.CompletedProcess[str]:
    allowed = _ALLOWED_GIT | _COMMIT_GIT if _commit else _ALLOWED_GIT
    if not args or args[0] not in allowed:
        raise WorktreeError(f"git {args[0] if args else ''} is not allowed for sessions")
    _EMPTY_HOOKS.mkdir(exist_ok=True)
    cmd = ["git", "-c", f"core.hooksPath={_EMPTY_HOOKS}", "-c", "credential.helper=",
           "-c", "protocol.allow=never", "-c", "core.fsmonitor=false",
           "-c", "diff.external=", *[x for kv in config for x in ("-c", kv)], *args]
    r = subprocess.run(cmd, cwd=str(cwd), env=_env(), capture_output=True, text=True,
                       timeout=timeout)
    if check and r.returncode != 0:
        raise WorktreeError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def git_version(cwd: str | Path = ".") -> tuple[int, ...]:
    out = _git(cwd, "version").stdout
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
    if not m:
        raise WorktreeError(f"unrecognised git version: {out.strip()}")
    return tuple(int(x) for x in m.groups(default="0"))


# ── naming & layout ────────────────────────────────────────────────────────

def default_root() -> Path:
    env = os.environ.get(WORKTREES_ENV)
    if env:
        return Path(env).expanduser()
    from .. import db  # lazy: reuse the resolved data dir
    return Path(db._DB_DIR) / "worktrees"


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40]
    return s or fallback


def repo_toplevel(repo_path: str | Path) -> Path:
    p = Path(repo_path).expanduser()
    if not p.is_dir():
        raise WorktreeError(f"repo not found: {repo_path}")
    top = _git(p, "rev-parse", "--show-toplevel").stdout.strip()
    return Path(top).resolve()


def branch_name(role: str, session_id: int) -> str:
    if not isinstance(session_id, int) or session_id < 1:
        raise WorktreeError("session_id must be a positive integer")
    return f"{BRANCH_PREFIX}{_slug(role, 'agent')}/{session_id}"


def worktree_path(repo_path: str | Path, session_id: int, root: str | Path | None = None) -> Path:
    top = repo_toplevel(repo_path)
    base = Path(root).expanduser() if root else default_root()
    return (base.resolve() / _slug(top.name, "repo") / str(session_id))


def _check_branch(repo: Path, branch: str, base_ref: str) -> None:
    short = branch.removeprefix("refs/heads/")
    if not short.startswith(BRANCH_PREFIX) or short in PROTECTED_BRANCHES \
            or short == base_ref.removeprefix("refs/heads/"):
        raise WorktreeError(f"refusing branch {short!r}: sessions only use {BRANCH_PREFIX}* branches")
    _git(repo, "check-ref-format", "--branch", short)


def _check_outside_repo(repo: Path, path: Path) -> None:
    rp, p = repo.resolve(), path.resolve()
    if p == rp or rp in p.parents or p in rp.parents:
        raise WorktreeError("worktrees must live outside the repo and never be the main checkout")


def _registered(repo: Path) -> list[dict[str, str]]:
    """``git worktree list --porcelain`` as dicts; the first is the main checkout."""
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    items: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in out.splitlines():
        if not line:
            if cur:
                items.append(cur)
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v
    if cur:
        items.append(cur)
    return items


def _ours(repo: Path, path: str | Path, root: str | Path | None) -> tuple[Path, dict[str, str]]:
    p = Path(path).expanduser().resolve()
    base = (Path(root).expanduser() if root else default_root()).resolve()
    if base not in p.parents:
        raise WorktreeError("not a session worktree (outside the worktrees root)")
    reg = _registered(repo)
    if reg and Path(reg[0].get("worktree", "")).resolve() == p:
        raise WorktreeError("refusing to operate on the main checkout")
    for wt in reg[1:]:
        if Path(wt.get("worktree", "")).resolve() == p:
            branch = wt.get("branch", "").removeprefix("refs/heads/")
            if not branch.startswith(BRANCH_PREFIX):
                raise WorktreeError(f"worktree is on {branch or 'a detached HEAD'}, not a session branch")
            return p, wt
    raise WorktreeError("not a registered worktree of this repo")


def assert_session_worktree(repo_path: str | Path, path: str | Path, *,
                            root: str | Path | None = None) -> tuple[Path, str]:
    """Raise unless ``path`` is one of OUR session worktrees of ``repo_path``
    (under the root, registered, on a ``cl/`` branch). Returns (path, branch)."""
    p, wt = _ours(repo_toplevel(repo_path), path, root)
    return p, wt.get("branch", "").removeprefix("refs/heads/")


def head_file(worktree: str | Path, rel: str) -> tuple[bytes, bool] | None:
    """HEAD's ``(content, executable)`` for ``rel``, or None if HEAD has no such
    file. Read-only; bytes, so binary files survive (``_git`` is text-mode)."""
    _EMPTY_HOOKS.mkdir(exist_ok=True)
    base = ["git", "-c", f"core.hooksPath={_EMPTY_HOOKS}", "-c", "core.fsmonitor=false"]
    r = subprocess.run([*base, "cat-file", "blob", f"HEAD:{rel}"], cwd=str(worktree),  # noqa: S603
                       env=_env(), capture_output=True, timeout=60)
    if r.returncode != 0:
        return None
    mode = _git(worktree, "ls-files", "-s", "--", rel, check=False).stdout.split(" ", 1)[0]
    return r.stdout, mode == "100755"


def list_files(path: str | Path) -> list[str]:
    """Tracked + untracked-not-ignored files of a worktree (repo-relative)."""
    out = _git(path, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    return sorted({f for f in out.split("\0") if f})


AGENT_NAME = "ConductLoop agent"
AGENT_EMAIL = "agent@conductloop.invalid"


def commit_paths(worktree: str | Path, paths_: list[str], message: str) -> str | None:
    """Commit exactly ``paths_`` on the worktree's own ``cl/`` branch (local
    only: never push, never merge). Author/committer is the agent identity —
    never the user's — with signing and hooks off. Returns the new commit SHA,
    or None when there was nothing to commit. Callers pass a worktree already
    verified with ``assert_session_worktree``."""
    wt = Path(worktree)
    branch = _git(wt, "symbolic-ref", "--short", "HEAD").stdout.strip()
    if not branch.startswith(BRANCH_PREFIX):
        raise WorktreeError(f"refusing to commit on {branch!r}")
    clean = [p for p in paths_ if p and not p.startswith("-")]
    if not clean:
        return None
    _git(wt, "add", "-A", "--", *clean, _commit=True)
    if _git(wt, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    ident = (f"user.name={AGENT_NAME}", f"user.email={AGENT_EMAIL}", "commit.gpgsign=false")
    _git(wt, "commit", "-q", "--no-verify", "-m", message, config=ident, _commit=True)
    return _git(wt, "rev-parse", "HEAD").stdout.strip()


# ── operations ─────────────────────────────────────────────────────────────

def create(repo_path: str | Path, base_ref: str, *, session_id: int, role: str,
           root: str | Path | None = None) -> WorktreeInfo:
    """``git worktree add -b cl/<role>/<id> <root>/<slug>/<id> <base_ref>``."""
    repo = repo_toplevel(repo_path)
    if not base_ref or base_ref.startswith("-"):
        raise WorktreeError("base_ref is required")
    base_commit = _git(repo, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}",
                       check=False)
    if base_commit.returncode != 0:
        raise WorktreeError(f"base_ref not found: {base_ref}")
    branch = branch_name(role, session_id)
    _check_branch(repo, branch, base_ref)
    if _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
            check=False).returncode == 0:
        raise WorktreeError(f"branch {branch} already exists")
    path = worktree_path(repo, session_id, root)
    _check_outside_repo(repo, path)
    if path.exists():
        raise WorktreeError(f"worktree path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", branch, str(path), base_commit.stdout.strip())
    return inspect(repo, path, base_ref, root=root)


def inspect(repo_path: str | Path, path: str | Path, base_ref: str, *,
            root: str | Path | None = None) -> WorktreeInfo:
    repo = repo_toplevel(repo_path)
    p, wt = _ours(repo, path, root)
    branch = wt.get("branch", "").removeprefix("refs/heads/")
    status = _git(p, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    changed = [e[3:] for e in status.split("\0") if len(e) > 3]
    ahead = int(_git(p, "rev-list", "--count", f"{base_ref}..HEAD").stdout.strip() or 0)
    files = [f for f in _git(p, "diff", "--name-only", "-z", f"{base_ref}...HEAD")
             .stdout.split("\0") if f]
    return WorktreeInfo(path=str(p), branch=branch, base_ref=base_ref,
                        head=wt.get("HEAD", ""), dirty=bool(changed), changed=changed,
                        ahead=ahead, files_vs_base=files)


def prune(repo_path: str | Path, path: str | Path, base_ref: str, *,
          root: str | Path | None = None) -> PruneResult:
    """Remove the worktree and its branch ONLY if it is clean and has no commits
    beyond ``base_ref``; otherwise keep both and say why ("para revisar")."""
    repo = repo_toplevel(repo_path)
    info = inspect(repo, path, base_ref, root=root)
    if info.dirty:
        return PruneResult(False, "kept: uncommitted changes", info)
    if info.ahead:
        return PruneResult(False, f"kept: {info.ahead} commit(s) to review on {info.branch}", info)
    _git(repo, "worktree", "remove", info.path)          # no --force: git refuses if dirty
    # Delete the branch only if it still points at the commit inspected above
    # (ahead == 0): a commit made in between makes update-ref refuse, and the
    # branch is kept for review instead of being lost.
    _check_branch(repo, info.branch, base_ref)
    if _git(repo, "update-ref", "-d", f"refs/heads/{info.branch}", info.head,
            check=False).returncode != 0:
        return PruneResult(False, f"kept: {info.branch} moved while pruning", info)
    return PruneResult(True, "removed: clean and no commits", info)


def dry_merge(repo_path: str | Path, base_ref: str, branch: str) -> DryMergeResult:
    """Simulate merging ``branch`` into ``base_ref`` without touching anything."""
    repo = repo_toplevel(repo_path)
    if git_version(repo) < MIN_MERGE_TREE_GIT:
        raise WorktreeError("git >= 2.38 is required for merge-tree --write-tree")
    for ref in (base_ref, branch):
        if not ref or ref.startswith("-"):
            raise WorktreeError("refs are required")
    r = _git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages",
             base_ref, branch, check=False)
    if r.returncode not in (0, 1):
        raise WorktreeError(f"git merge-tree failed: {r.stderr.strip()[:300]}")
    lines = [ln for ln in r.stdout.splitlines() if ln]
    tree = lines[0] if lines else None
    conflicts = sorted(set(lines[1:])) if r.returncode == 1 else []
    return DryMergeResult(clean=r.returncode == 0, conflicts=conflicts, tree=tree)


def list_session_worktrees(repo_path: str | Path, *,
                           root: str | Path | None = None) -> list[dict[str, str]]:
    repo = repo_toplevel(repo_path)
    base = (Path(root).expanduser() if root else default_root()).resolve()
    out = []
    for wt in _registered(repo)[1:]:
        p = Path(wt.get("worktree", "")).resolve()
        if base in p.parents and wt.get("branch", "").removeprefix("refs/heads/").startswith(BRANCH_PREFIX):
            out.append({"path": str(p), "branch": wt["branch"].removeprefix("refs/heads/"),
                        "head": wt.get("HEAD", "")})
    return out
