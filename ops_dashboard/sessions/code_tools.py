"""Code tools for a session agent, confined to the session's worktree
(multi-agent PR 5; design doc D2/D3).

``CodeTools`` exposes read / list / search / write / edit / diff / run.

Confinement (every path argument):
  * repo-relative only: absolute paths, ``..`` components, NUL, backslashes
    and ``~`` are refused before touching the filesystem;
  * any ``.git`` component is refused (case-insensitive, Unicode-normalised,
    ignorable code points stripped — HFS+ treats ``.g\u200cit`` as ``.git``);
  * the path is resolved with ``realpath`` and must stay inside the worktree's
    realpath — a symlink pointing outside is refused, for reads and writes;
  * writes never go through a symlink (the final component may not be one)
    and are atomic (temp file + ``os.replace`` in the same directory).

Writes (D3) need, at the moment of writing: the session active, the task
owned by this session and in flight, an active reservation of this session
(``SessionStore.can_write``) covering the exact path, and NO active
reservation of another session — or an exclusive one of another task —
covering it. Hot files / lockfiles need an exclusive reservation. Claims made
through ``claim`` use ``paths.precise_overlap`` over ``git ls-files`` to avoid
#18's prefix-based false positives; the per-path check above keeps that safe.

``run`` only accepts whitelisted test/lint invocations (pytest, ruff check /
format --check|--diff, mypy, npm test / npm run lint|test) with validated
arguments; no installs, no fixes that write, no config/plugin overrides. It
executes through ``sandbox.run`` (see that module for what is and isn't
guaranteed, notably about the network).

Every call — allowed or denied — appends a ``tool.*`` event to
``session_events`` with metadata only (path, sizes, exit code, duration,
sandbox), never file contents or command output.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import stat
import sys
import time
import unicodedata
from collections.abc import Callable, Iterable
from functools import partial
from pathlib import Path
from typing import Any

from . import limits, paths, sandbox, worktrees
from .store import SessionStore

MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_LIST_ENTRIES = 2000
MAX_SEARCH_MATCHES = 200
MAX_DIFF_BYTES = 128 * 1024
MAX_ARGS = 64
MAX_ARG_LEN = 512

_IGNORABLE = re.compile("[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f"
                        "\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0]")


class ToolDenied(PermissionError):
    """The call was refused by policy. Message is safe to show to the agent."""


class ToolError(RuntimeError):
    """The call was allowed but failed (missing file, bad edit…)."""


def _is_git_component(part: str) -> bool:
    p = unicodedata.normalize("NFC", _IGNORABLE.sub("", part)).casefold().rstrip(". ")
    return p == ".git"


def clean_relpath(path: str) -> str:
    """Validate a repo-relative path lexically. '' / '.' mean the root."""
    if not isinstance(path, str):
        raise ToolDenied("path must be a string")
    if "\0" in path or "\\" in path or len(path) > 1024:
        raise ToolDenied("invalid characters in path")
    p = path.strip()
    if p.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", p):
        raise ToolDenied("absolute paths are not allowed; use a path relative to the worktree")
    parts = [s for s in p.split("/") if s not in ("", ".")]
    if any(s == ".." for s in parts):
        raise ToolDenied("'..' is not allowed in paths")
    if any(_is_git_component(s) for s in parts):
        raise ToolDenied(".git is off limits")
    return "/".join(parts)


# ── whitelisted commands ───────────────────────────────────────────────────

_EXPR_RE = re.compile(r"^[\w\s.\-\[\]():,'\"=/]{1,200}$")
_RULES_RE = re.compile(r"^[A-Z]{1,4}[0-9]{0,4}(,[A-Z]{1,4}[0-9]{0,4})*$")
_INT_RE = re.compile(r"^[0-9]{1,4}$")


def _choice(*opts: str) -> Callable[[str], bool]:
    return lambda v: v in opts


def _re(pattern: re.Pattern[str]) -> Callable[[str], bool]:
    return lambda v: pattern.match(v) is not None


def _numbered(lines: list[str], first: int) -> str:
    """The lines of a file, with the number of each one."""
    return "".join(f"{first + i}: {line}\n" for i, line in enumerate(lines))


def _closest_lines(text: str, old: str, limit: int = 5) -> str:
    """The lines of the file that look the most like the text that was not found.

    The model wrote the old text from memory, or from a version of the file
    that has changed. Showing it the lines that look like it — with their
    numbers — is how it fixes its own call.
    """
    wanted = [ln.strip() for ln in old.splitlines() if ln.strip()]
    if not wanted:
        return ""
    target = wanted[0]
    scored: list[tuple[float, int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        ratio = difflib.SequenceMatcher(None, target, stripped).ratio()
        if ratio >= 0.45:
            scored.append((ratio, i, line.rstrip()))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return "\n".join(f"{i}: {line}" for _, i, line in scored[:limit])


def _patch_paths(patch: str) -> list[str]:
    """The files a unified diff touches, from its headers.

    Both headers: a diff that deletes a file says ``/dev/null`` in the
    ``+++`` line, and the file it deletes has to be reserved too.
    """
    out: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            target = line[4:].strip()
            if target == "/dev/null":
                continue
            if target.startswith("a/") or target.startswith("b/"):
                target = target[2:]
            target = target.split("\t", 1)[0].strip()
            if target and target not in out:
                out.append(target)
    return out


# tool -> (flags without value, flags with value -> validator)
_SPECS: dict[str, tuple[frozenset[str], dict[str, Callable[[str], bool]]]] = {
    "pytest": (frozenset({"-q", "-qq", "-v", "-vv", "-x", "-s", "-l", "-ra", "-rA", "-rf",
                          "--showlocals", "--no-header", "--collect-only", "--co",
                          "--strict-markers", "--exitfirst", "--quiet", "--verbose"}),
               {"-k": _re(_EXPR_RE), "-m": _re(_EXPR_RE), "--maxfail": _re(_INT_RE),
                "--durations": _re(_INT_RE),
                "--tb": _choice("auto", "long", "short", "line", "native", "no")}),
    "ruff check": (frozenset({"--statistics", "-q", "--quiet", "--show-fixes", "--no-fix",
                              "--preview"}),
                   {"--select": _re(_RULES_RE), "--ignore": _re(_RULES_RE),
                    "--extend-select": _re(_RULES_RE),
                    "--output-format": _choice("concise", "full", "json", "grouped", "github")}),
    "ruff format": (frozenset({"--check", "--diff", "-q", "--quiet"}), {}),
    "mypy": (frozenset({"--strict", "--ignore-missing-imports", "--no-error-summary",
                        "--pretty", "--show-error-codes", "--no-incremental",
                        "--check-untyped-defs", "--warn-unused-ignores"}),
             {"--follow-imports": _choice("normal", "silent", "skip", "error"),
              "--python-version": _re(re.compile(r"^3\.[0-9]{1,2}$"))}),
}
_NPM_ALLOWED = (("test",), ("run", "test"), ("run", "lint"))

# The git commands an agent may run: the ones that only read the history.
# Nothing that writes (no commit, no checkout, no stash), nothing that runs
# anything else (no --ext-diff, no --output, no -c).
_GIT_READONLY: dict[str, tuple[frozenset[str], dict[str, Callable[[str], bool]]]] = {
    "log": (frozenset({"--oneline", "--stat", "--patch", "-p", "--name-only", "--name-status",
                       "--decorate", "--no-color", "--graph", "--all", "--follow", "--reverse",
                       "--first-parent", "--merges", "--no-merges", "--author"}),
            {"-n": _re(_INT_RE), "--max-count": _re(_INT_RE),
             "--since": _re(_EXPR_RE), "--until": _re(_EXPR_RE)}),
    "show": (frozenset({"--stat", "--name-only", "--name-status", "--no-color", "--patch",
                         "-p", "--format"}), {}),
    "blame": (frozenset({"-w", "--line-porcelain", "--show-stats"}), {}),
    "diff": (frozenset({"--stat", "--name-only", "--name-status", "--no-color"}),
             {"-U": _re(_INT_RE), "--unified": _re(_INT_RE)}),
}
# A revision: what a ref, a range or a path may look like. Read-only, so
# the worst a wrong one can do is fail.
_GIT_REV_RE = re.compile(r"^[A-Za-z0-9_./~^@{}\-]+$")
_PY_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# The modules that install or build something. The sandbox has no network, so
# they could not download anything, but "python -m pip install" has no reason
# to be allowed in a session that may not install anything.
_PY_BLOCKED_MODULES = frozenset({
    "pip", "ensurepip", "easy_install", "setuptools", "distutils", "wheel",
    "conda", "mamba", "poetry", "pipenv", "uv", "hatch", "pdm", "flit",
    "venv", "virtualenv",
})


class CodeTools:
    def __init__(self, store: SessionStore, session_id: int, task_id: int, *,
                 worktrees_root: str | Path | None = None, python: str | None = None,
                 timeout_s: float = sandbox.DEFAULT_TIMEOUT_S,
                 max_output: int = sandbox.DEFAULT_MAX_OUTPUT,
                 max_file_bytes: int = sandbox.DEFAULT_MAX_FILE_BYTES,
                 sandbox_backend: str | None = None, allow_unsandboxed: bool = False):
        self.store, self.session_id, self.task_id = store, int(session_id), int(task_id)
        s = store.get_session(self.session_id)
        if s is None or s["status"] not in limits.ACTIVE_SESSION_STATUSES:
            raise ToolDenied("no such active session")
        if not s["worktree_path"]:
            raise ToolDenied("session has no worktree")
        self.repo_path = s["repo_path"]
        root, branch = worktrees.assert_session_worktree(self.repo_path, s["worktree_path"],
                                                         root=worktrees_root)
        if s["branch"] and branch != s["branch"]:
            raise ToolDenied("worktree branch does not match the session")
        # The row's worktree/branch are only data: they must be the ones this
        # session id derives, or a session could be pointed at another's checkout.
        try:
            expected_wt = worktrees.worktree_path(self.repo_path, self.session_id, worktrees_root)
            expected_branch = worktrees.branch_name(s["role"], self.session_id)
        except worktrees.WorktreeError as e:
            raise ToolDenied(str(e)) from e
        if Path(os.path.realpath(root)) != Path(os.path.realpath(expected_wt)) \
                or branch != expected_branch:
            raise ToolDenied("worktree does not belong to this session")
        self.root = Path(os.path.realpath(root))
        # The worktree's `.git` is a plain file the agent's code could rewrite to
        # aim our git calls at another repo: pin the gitdir and re-check it.
        self._gitdir = self._current_gitdir()
        self.base_ref = s["base_ref"]
        self._python = python
        self.timeout_s, self.max_output, self.max_file_bytes = timeout_s, max_output, max_file_bytes
        self.sandbox_backend, self.allow_unsandboxed = sandbox_backend, allow_unsandboxed

    def _current_gitdir(self) -> str:
        return worktrees._git(self.root, "rev-parse", "--absolute-git-dir").stdout.strip()

    def _git(self, *args: str, check: bool = True, timeout: int = 60,
             stdin: str | None = None):  # type: ignore[no-untyped-def]
        if self._current_gitdir() != self._gitdir:
            raise ToolDenied("the worktree's .git link was tampered with")
        return worktrees._git(self.root, *args, check=check, timeout=timeout, stdin=stdin)

    # ── bookkeeping ─────────────────────────────────────────────────────
    def _event(self, tool: str, ok: bool, **meta: Any) -> None:
        self.store.record_event(self.session_id, f"tool.{tool}", {"ok": ok, **meta},
                                task_id=self.task_id)

    def _guard(self, tool: str, fn: Callable[[], dict], **meta: Any) -> dict:
        t0 = time.monotonic()
        try:
            out = fn()
        except (ToolDenied, ToolError, sandbox.SandboxUnavailable) as e:
            self._event(tool, False, denied=isinstance(e, (ToolDenied, sandbox.SandboxUnavailable)),
                        error=str(e)[:300], **meta)
            raise
        meta.update(out.pop("_meta", {}))
        self._event(tool, True, ms=int((time.monotonic() - t0) * 1000), **meta)
        return out

    # ── confinement ─────────────────────────────────────────────────────
    def resolve(self, path: str, *, for_write: bool = False) -> tuple[str, Path]:
        rel = clean_relpath(path)
        lexical = self.root / rel if rel else self.root
        real = Path(os.path.realpath(lexical))
        if real != self.root and self.root not in real.parents:
            raise ToolDenied("path resolves outside the worktree (symlink?)")
        real_rel = real.relative_to(self.root).as_posix() if real != self.root else ""
        if any(_is_git_component(s) for s in real_rel.split("/") if s):
            raise ToolDenied(".git is off limits")
        if for_write:
            if not rel:
                raise ToolDenied("cannot write the worktree root")
            if os.path.islink(lexical):
                raise ToolDenied("refusing to write through a symlink")
            if real_rel != rel:
                raise ToolDenied("refusing to write through a symlinked directory")
        return rel, real

    def _check_task(self) -> None:
        s = self.store.get_session(self.session_id)
        if s is None or s["status"] not in limits.ACTIVE_SESSION_STATUSES:
            raise ToolDenied("session is not active")
        t = self.store.get_task(self.task_id)
        if t is None or t["owner_session_id"] != self.session_id \
                or t["status"] not in ("claimed", "running"):
            raise ToolDenied("task is not in flight for this session")

    def check_write(self, rel: str) -> None:
        """D3 at write time, on the concrete path."""
        self._check_task()
        if not self.store.can_write(self.session_id, rel, task_id=self.task_id):
            raise ToolDenied(f"no active reservation of this session/task covers {rel}")
        claims = self.store.active_claims(repo_path=self.repo_path)
        hot = paths.is_exclusive(rel)
        mine = [c for c in claims if c["session_id"] == self.session_id
                and c["task_id"] in (None, self.task_id) and paths.covers(c["pattern"], rel)]
        if hot and not any(c["mode"] == "exclusive" for c in mine):
            raise ToolDenied(f"{rel} needs an exclusive reservation")
        for c in claims:
            if not paths.covers(c["pattern"], rel) or c in mine:
                continue
            if c["session_id"] != self.session_id:
                raise ToolDenied(f"{rel} is reserved by session #{c['session_id']}")
            if c["mode"] == "exclusive" or hot:
                raise ToolDenied(f"{rel} is reserved exclusively by task #{c['task_id']}")

    # ── reservations ────────────────────────────────────────────────────
    def claim(self, patterns: Iterable[str], *, ttl_seconds: int | None = None) -> list[dict]:
        pats = list(patterns)

        def go() -> dict:
            self._check_task()
            self._git("rev-parse", "HEAD")  # gitdir pin check
            files = worktrees.list_files(self.root)
            granted = self.store.claim_paths(
                self.session_id, pats, task_id=self.task_id, ttl_seconds=ttl_seconds,
                overlap=partial(paths.precise_overlap, files=files))
            return {"claims": granted}
        try:
            return self._guard("claim", go, patterns=pats)["claims"]
        except paths.PathConflictError as e:
            self._event("claim", False, denied=True, error=str(e)[:300], patterns=pats)
            raise
        except ValueError as e:   # "../x", a foreign task… (SessionValidationError): audited too
            self._event("claim", False, denied=True, error=str(e)[:300],
                        patterns=[str(p)[:200] for p in pats])
            raise ToolDenied(str(e)[:300]) from e

    # ── read side ───────────────────────────────────────────────────────
    def read_file(self, path: str, *, offset: int = 0, limit: int = MAX_READ_BYTES,
                  start_line: int | None = None, end_line: int | None = None) -> dict:
        """Read a file, with its line numbers.

        The numbers are what the model needs to talk about a place in the
        file ("the limit is at line 19") and to build an edit. A range
        (``start_line``/``end_line``, 1-based and inclusive) reads only that
        part; ``offset`` stays a byte offset for the rare case that needs it.
        """
        def go() -> dict:
            rel, real = self.resolve(path)
            if not real.is_file():
                raise ToolError(f"not a file: {rel or '.'}")
            size = real.stat().st_size
            if start_line is not None or end_line is not None:
                first = max(1, int(start_line or 1))
                last = int(end_line) if end_line is not None else first + 199
                if last < first:
                    raise ToolError("end_line is before start_line")
                data = real.read_bytes()
                if b"\0" in data[:8192]:
                    return {"path": rel, "size": size, "binary": True, "text": None,
                            "start_line": first, "end_line": last, "total_lines": None,
                            "truncated": False, "_meta": {"bytes": len(data)}}
                lines = data.decode("utf-8", "replace").splitlines()
                chosen = lines[first - 1:last]
                return {"path": rel, "size": size, "binary": False,
                        "start_line": first, "end_line": first + len(chosen) - 1,
                        "total_lines": len(lines),
                        "text": _numbered(chosen, first),
                        "truncated": last < len(lines),
                        "_meta": {"lines": len(chosen)}}
            n = max(0, min(int(limit), MAX_READ_BYTES))
            start = max(0, int(offset))
            with open(real, "rb") as f:
                prefix = f.read(start) if start else b""
                data = f.read(n)
            binary = b"\0" in data[:8192]
            if binary:
                return {"path": rel, "size": size, "offset": offset, "binary": True,
                        "truncated": start + len(data) < size, "text": None,
                        "_meta": {"bytes": len(data)}}
            text = data.decode("utf-8", "replace")
            first_line = prefix.count(b"\n") + 1
            return {"path": rel, "size": size, "offset": offset, "binary": False,
                    "start_line": first_line, "total_lines": None,
                    "truncated": start + len(data) < size,
                    "text": _numbered(text.splitlines(), first_line),
                    "_meta": {"bytes": len(data)}}
        return self._guard("read", go, path=path)

    def list_dir(self, path: str = ".", *, recursive: bool = False,
                 max_entries: int = MAX_LIST_ENTRIES) -> dict:
        def go() -> dict:
            rel, real = self.resolve(path)
            if not real.is_dir():
                raise ToolError(f"not a directory: {rel or '.'}")
            cap = max(1, min(int(max_entries), MAX_LIST_ENTRIES))
            entries: list[dict] = []
            stack = [real]
            while stack and len(entries) < cap:
                d = stack.pop()
                with os.scandir(d) as it:
                    for e in sorted(it, key=lambda x: x.name):
                        if _is_git_component(e.name):
                            continue
                        r = Path(e.path).relative_to(self.root).as_posix()
                        if e.is_symlink():
                            kind = "symlink"
                        elif e.is_dir(follow_symlinks=False):
                            kind = "dir"
                            if recursive:
                                stack.append(Path(e.path))
                        else:
                            kind = "file"
                        entries.append({"path": r, "type": kind,
                                        "size": e.stat(follow_symlinks=False).st_size
                                        if kind == "file" else None})
                        if len(entries) >= cap:
                            break
            return {"path": rel or ".", "entries": entries, "truncated": len(entries) >= cap,
                    "_meta": {"count": len(entries)}}
        return self._guard("list", go, path=path, recursive=recursive)

    def search(self, query: str, path: str = ".", *, regex: bool = False,
               ignore_case: bool = False, max_matches: int = MAX_SEARCH_MATCHES) -> dict:
        def go() -> dict:
            if not isinstance(query, str) or not query or len(query) > 500 or "\0" in query \
                    or "\n" in query:
                raise ToolDenied("query must be a single line of 1-500 characters")
            rel, _ = self.resolve(path)
            args = ["grep", "-n", "-I", "--no-color", "--untracked", "-E" if regex else "-F"]
            if ignore_case:
                args.append("-i")
            args += ["-e", query, "--", f":(literal){rel}" if rel else "."]
            r = self._git(*args, check=False, timeout=30)
            if r.returncode not in (0, 1):
                raise ToolError(f"search failed: {r.stderr.strip()[:200]}")
            cap = max(1, min(int(max_matches), MAX_SEARCH_MATCHES))
            matches = []
            for line in r.stdout.splitlines():
                f, _, rest = line.partition(":")
                ln, _, text = rest.partition(":")
                matches.append({"path": f, "line": int(ln) if ln.isdigit() else None,
                                "text": text[:300]})
                if len(matches) >= cap:
                    break
            return {"matches": matches, "truncated": len(r.stdout.splitlines()) > cap,
                    "_meta": {"count": len(matches)}}
        return self._guard("search", go, path=path, regex=regex,
                           query_sha256=hashlib.sha256(str(query).encode()).hexdigest()[:16])

    def diff(self, *, against: str = "head") -> dict:
        def go() -> dict:
            if against not in ("head", "base"):
                raise ToolDenied("against must be 'head' or 'base'")
            ref = "HEAD" if against == "head" else self.base_ref
            if not ref or ref.startswith("-"):
                raise ToolDenied("invalid base ref")
            st = self._git("status", "--porcelain=v1", "-z",
                                "--untracked-files=all").stdout
            changed = [{"status": e[:2], "path": e[3:]} for e in st.split("\0") if len(e) > 3]
            d = self._git("diff", "--no-ext-diff", "--no-textconv", "--no-color",
                               ref, "--").stdout
            data = d.encode()
            return {"against": against, "changed": changed,
                    "diff": data[:MAX_DIFF_BYTES].decode("utf-8", "replace"),
                    "truncated": len(data) > MAX_DIFF_BYTES,
                    "_meta": {"files": len(changed), "bytes": len(data)}}
        return self._guard("diff", go, against=against)

    # ── write side ──────────────────────────────────────────────────────
    def _atomic_write(self, rel: str, real: Path, data: bytes) -> None:
        parent = real.parent
        if not parent.exists():
            parent.mkdir(parents=True)
            # mkdir followed whatever existed: re-verify the final location.
            self.resolve(rel, for_write=True)
        mode = None
        if os.path.lexists(real):
            st = os.lstat(real)
            if not stat.S_ISREG(st.st_mode):
                raise ToolDenied(f"{rel} is not a regular file")
            mode = stat.S_IMODE(st.st_mode)
        dfd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            pst = os.fstat(dfd)
            cst = os.stat(parent)
            if (pst.st_dev, pst.st_ino) != (cst.st_dev, cst.st_ino):
                raise ToolDenied("directory changed during write")
            tmp = f".cl-tmp-{os.getpid()}-{time.monotonic_ns()}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o644 if mode is None else mode, dir_fd=dfd)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, real.name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=dfd)
                except OSError:
                    pass
                raise
        finally:
            os.close(dfd)

    def write_file(self, path: str, content: str) -> dict:
        def go() -> dict:
            if not isinstance(content, str):
                raise ToolDenied("content must be text")
            data = content.encode("utf-8")
            if len(data) > MAX_WRITE_BYTES:
                raise ToolDenied(f"content exceeds {MAX_WRITE_BYTES} bytes")
            rel, real = self.resolve(path, for_write=True)
            self.check_write(rel)
            existed = os.path.lexists(real)
            self._atomic_write(rel, real, data)
            return {"path": rel, "bytes": len(data), "created": not existed,
                    "_meta": {"bytes": len(data), "created": not existed}}
        return self._guard("write", go, path=path)

    def edit_file(self, path: str, old: str, new: str, *, count: int = 1) -> dict:
        """Replace a piece of text.

        When the text is not there, the error says which lines of the file
        look like it, with their numbers: the model can correct itself in
        one more turn instead of reading the file again.
        """
        def go() -> dict:
            if not isinstance(old, str) or not old or not isinstance(new, str):
                raise ToolDenied("old must be non-empty text and new must be text")
            rel, real = self.resolve(path, for_write=True)
            self.check_write(rel)
            if not real.is_file():
                raise ToolError(f"not a file: {rel}")
            if real.stat().st_size > MAX_WRITE_BYTES:
                raise ToolDenied(f"{rel} is larger than {MAX_WRITE_BYTES} bytes")
            text = real.read_text(encoding="utf-8")
            found = text.count(old)
            if found != count:
                hints = _closest_lines(text, old)
                raise ToolError(f"expected {count} occurrence(s) of the old text, found {found}"
                                + (f". The lines that look like it:\n{hints}" if hints else ""))
            data = text.replace(old, new).encode("utf-8")
            if len(data) > MAX_WRITE_BYTES:
                raise ToolDenied(f"result exceeds {MAX_WRITE_BYTES} bytes")
            self._atomic_write(rel, real, data)
            return {"path": rel, "replacements": found, "bytes": len(data),
                    "_meta": {"bytes": len(data), "replacements": found}}
        return self._guard("edit", go, path=path)

    # ── patch (several changes at once) ─────────────────────────────────
    def apply_patch(self, patch: str) -> dict:
        """Apply a unified diff.

        One edit per turn is one edit per turn. A change that touches four
        places needs four turns, and the model loses the thread. A unified
        diff does them all at once.
        """
        def go() -> dict:
            if not isinstance(patch, str) or not patch.strip():
                raise ToolDenied("patch must be a unified diff")
            if len(patch.encode("utf-8")) > MAX_WRITE_BYTES:
                raise ToolDenied(f"patch exceeds {MAX_WRITE_BYTES} bytes")
            paths = _patch_paths(patch)
            if not paths:
                raise ToolError("the patch does not touch any file")
            for p in paths:
                rel, _ = self.resolve(p, for_write=True)
                self.check_write(rel)
            before = self._dirty()
            try:
                self._git("apply", "--whitespace=nowarn", "-", stdin=patch, timeout=60)
            except worktrees.WorktreeError as e:
                raise ToolError(f"the patch did not apply: {e}"[:300]) from e
            after = self._dirty()
            changed = sorted(p for p in set(before) | set(after)
                             if before.get(p, "<clean>") != after.get(p, "<clean>"))
            return {"paths": changed, "files": len(changed),
                    "_meta": {"files": len(changed)}}
        return self._guard("patch", go)

    # ── run ─────────────────────────────────────────────────────────────
    def python(self) -> str:
        if self._python:
            return self._python
        venv = Path(self.repo_path) / ".venv" / "bin" / "python"
        return str(venv) if venv.exists() else sys.executable

    def _validate_paths(self, values: list[str], *, nodeids: bool) -> list[str]:
        out = []
        for v in values:
            if v.startswith(("-", "@")):
                raise ToolDenied(f"unexpected option {v!r}")
            p, sep, node = v.partition("::") if nodeids else (v, "", "")
            if node and not re.match(r"^[\w\[\]\-.:,]+$", node):
                raise ToolDenied("invalid test node id")
            rel, real = self.resolve(p)
            if not real.exists():
                raise ToolDenied(f"no such path: {rel or '.'}")
            out.append((rel or ".") + sep + node)
        return out

    def _parse(self, spec: str, args: list[str], *, nodeids: bool) -> list[str]:
        noarg, valued = _SPECS[spec]
        out: list[str] = []
        positional: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in noarg:
                out.append(a)
            elif a.startswith("-"):
                name, eq, val = a.partition("=")
                if name not in valued:
                    raise ToolDenied(f"option {name!r} is not allowed for {spec}")
                if not eq:
                    i += 1
                    if i >= len(args):
                        raise ToolDenied(f"option {name} needs a value")
                    val = args[i]
                if not valued[name](val):
                    raise ToolDenied(f"invalid value for {name}")
                out += [name, val]
            else:
                positional.append(a)
            i += 1
        return out + self._validate_paths(positional, nodeids=nodeids)

    def build_command(self, argv: list[str]) -> tuple[list[str], list[str]]:
        """Validate ``argv`` against the whitelist. Returns (exec argv, PATH dirs)."""
        if not isinstance(argv, list) or not argv or len(argv) > MAX_ARGS:
            raise ToolDenied("command must be a non-empty list of arguments")
        for a in argv:
            if not isinstance(a, str) or not a or len(a) > MAX_ARG_LEN \
                    or any(ord(ch) < 32 for ch in a):
                raise ToolDenied("invalid argument")
        py = self.python()
        py_dirs = [str(Path(py).parent)]
        tool, rest = argv[0], argv[1:]
        if tool in ("python", "python3"):
            return self._build_python(rest, py, py_dirs)
        if tool == "pytest":
            return ([py, "-m", "pytest", "-p", "no:cacheprovider", "--rootdir", str(self.root),
                     *self._parse("pytest", rest, nodeids=True)], py_dirs)
        if tool == "ruff":
            sub = rest[:1]
            if sub == ["format"] and not ({"--check", "--diff"} & set(rest)):
                raise ToolDenied("ruff format needs --check or --diff (writes go through write/edit)")
            if sub not in (["check"], ["format"]):
                raise ToolDenied("only 'ruff check' and 'ruff format --check/--diff' are allowed")
            return ([py, "-m", "ruff", sub[0], "--no-cache",
                     *self._parse(f"ruff {sub[0]}", rest[1:], nodeids=False)], py_dirs)
        if tool == "mypy":
            return ([py, "-m", "mypy", *self._parse("mypy", rest, nodeids=False)], py_dirs)
        if tool == "npm":
            if tuple(rest) not in _NPM_ALLOWED:
                raise ToolDenied("only 'npm test', 'npm run test' and 'npm run lint' are allowed")
            npm = shutil.which("npm")
            if not npm:
                raise ToolError("npm not found")
            return ([npm, *rest], [str(Path(npm).parent), *py_dirs])
        if tool == "git":
            return self._build_git(rest, py_dirs)
        raise ToolDenied(f"{tool!r} is not in the allowed commands "
                         "(pytest, ruff, mypy, npm, python, git)")

    def _build_python(self, rest: list[str], py: str,
                      py_dirs: list[str]) -> tuple[list[str], list[str]]:
        """``python <file>`` and ``python -m <module>``.

        The sandbox already cuts the network and the writes outside the
        copy, so a script of the repository may run: the agent needs it to
        check something the tests do not cover.
        """
        if not rest:
            raise ToolDenied("python needs a file or a module")
        if rest[0] == "-m":
            if len(rest) < 2:
                raise ToolDenied("python -m needs a module")
            module, tail = rest[1], rest[2:]
            if module in ("pytest", "ruff", "mypy"):
                return self.build_command([module, *tail])
            if not _PY_MODULE_RE.match(module):
                raise ToolDenied("invalid module name")
            if module.split(".")[0] in _PY_BLOCKED_MODULES:
                raise ToolDenied(f"python -m {module} is not allowed: "
                                 "a session may not install anything")
            return ([py, "-m", module, *tail], py_dirs)
        if rest[0].startswith("-"):
            raise ToolDenied("only 'python <file>' and 'python -m <module>' are allowed")
        script, tail = rest[0], rest[1:]
        paths = self._validate_paths([script], nodeids=False)
        return ([py, *paths, *tail], py_dirs)

    def _build_git(self, rest: list[str], py_dirs: list[str]) -> tuple[list[str], list[str]]:
        """The git commands that only read: log, show, blame, diff."""
        git = shutil.which("git")
        if not git:
            raise ToolError("git not found")
        if not rest:
            raise ToolDenied("git needs a subcommand")
        sub, tail = rest[0], rest[1:]
        if sub not in _GIT_READONLY:
            raise ToolDenied("only 'git log', 'git show', 'git blame' and 'git diff' are allowed")
        noarg, valued = _GIT_READONLY[sub]
        flags: list[str] = []
        revs: list[str] = []
        paths: list[str] = []
        i = 0
        while i < len(tail):
            a = tail[i]
            if a == "--":
                paths = self._validate_paths(tail[i + 1:], nodeids=False)
                break
            if a in noarg:
                flags.append(a)
            elif a.startswith("-"):
                name, eq, val = a.partition("=")
                if name not in valued:
                    raise ToolDenied(f"option {name!r} is not allowed for git {sub}")
                if not eq:
                    i += 1
                    if i >= len(tail):
                        raise ToolDenied(f"option {name} needs a value")
                    val = tail[i]
                if not valued[name](val):
                    raise ToolDenied(f"invalid value for {name}")
                flags += [name, val]
            else:
                if not _GIT_REV_RE.match(a):
                    raise ToolDenied(f"invalid argument for git {sub}: {a!r}")
                revs.append(a)
            i += 1
        argv = worktrees.git_argv(sub, *flags, *revs)
        if paths:
            argv += ["--", *paths]
        return (argv, [str(Path(git).parent), *py_dirs])

    # ── what a command changed (the sandbox lets it write the whole worktree) ──
    def _file_state(self, rel: str) -> str | None:
        f = self.root / rel
        try:
            if f.is_symlink():
                return "link:" + os.readlink(f)
            return hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
        except OSError:
            return None

    def _dirty(self) -> dict[str, str | None]:
        """Uncommitted paths (tracked changes + untracked, not ignored) → state."""
        out = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        entries, res, i = out.split("\0"), {}, 0
        while i < len(entries):
            e = entries[i]
            if len(e) > 3:
                res[e[3:]] = self._file_state(e[3:])
                if e[0] in "RC" and i + 1 < len(entries):   # rename: the source follows
                    i += 1
                    res[entries[i]] = self._file_state(entries[i])
            i += 1
        return res

    def _restore(self, rel: str, before: dict[str, str | None], saved: dict[str, bytes]) -> None:
        f = self.root / rel
        if f.is_symlink() or (f.exists() and not f.is_dir()):
            f.unlink()
        if rel in saved:                       # was an uncommitted edit: put it back
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(saved[rel])
        elif rel not in before:                # was clean: back to HEAD (if tracked)
            self._git("rev-parse", "HEAD")     # gitdir pin check before reading from it
            head = worktrees.head_file(self.root, rel)
            if head is not None:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(head[0])
                f.chmod(0o755 if head[1] else 0o644)
        # else: it did not exist before (untracked new file / deleted edit): gone

    def run(self, argv: list[str], *, cwd: str = ".") -> dict:
        meta: dict[str, Any] = {"argv": [str(a)[:80] for a in argv[:MAX_ARGS]]
                                if isinstance(argv, list) else None}

        def go() -> dict:
            # pytest/npm execute repository code, which can write anywhere in the
            # worktree: the task must be in flight, and every file the command
            # changes is checked against this task's reservations afterwards.
            self._check_task()
            full, dirs = self.build_command(argv)
            _, real_cwd = self.resolve(cwd)
            if not real_cwd.is_dir():
                raise ToolDenied("cwd must be a directory in the worktree")
            before = self._dirty()
            saved = {}
            for rel, state in before.items():
                if state is not None and not state.startswith("link:"):
                    saved[rel] = (self.root / rel).read_bytes()
            r = sandbox.run(full, worktree=self.root, cwd=real_cwd, path_dirs=dirs,
                            timeout_s=self.timeout_s, max_output=self.max_output,
                            max_file_bytes=self.max_file_bytes, backend=self.sandbox_backend,
                            allow_unsandboxed=self.allow_unsandboxed)
            after = self._dirty()
            changed = sorted(p for p in set(before) | set(after)
                             if before.get(p, "<clean>") != after.get(p, "<clean>"))
            allowed, reverted = [], []
            for rel in changed:
                try:
                    clean_relpath(rel)
                    self.check_write(rel)
                    allowed.append(rel)
                except ToolDenied:
                    self._restore(rel, before, saved)
                    reverted.append(rel)
            out = {"exit_code": r.exit_code, "output": r.output, "truncated": r.truncated,
                   "output_bytes": r.output_bytes, "timed_out": r.timed_out,
                   "duration_ms": r.duration_ms, "sandbox": r.sandbox,
                   "changed_paths": allowed,
                   "_meta": {"exit_code": r.exit_code, "timed_out": r.timed_out,
                             "output_bytes": r.output_bytes, "sandbox": r.sandbox,
                             "changed": len(allowed), "reverted": reverted[:50]}}
            if reverted:
                out["reverted_unreserved"] = reverted[:200]
                out["warning"] = ("the command changed files outside this task's reservations; "
                                  "they were restored")
            return out
        return self._guard("run", go, **meta)
