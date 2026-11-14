"""sessions.worktrees against throwaway local git repos (no network)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ops_dashboard.sessions import worktrees as W

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}


def g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV, "HOME": str(cwd)}).stdout


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    r = tmp_path / "My Repo"
    r.mkdir()
    g(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("x = 1\n")
    (r / "README.md").write_text("hello\n")
    g(r, "add", ".")
    g(r, "commit", "-q", "-m", "init")
    root = tmp_path / "wt-root"
    monkeypatch.setenv(W.WORKTREES_ENV, str(root))
    return r, root


def commit_all(cwd, msg):
    g(cwd, "add", "-A")
    g(cwd, "commit", "-q", "-m", msg)


def test_create_layout_branch_outside_repo(repo):
    r, root = repo
    info = W.create(r, "main", session_id=7, role="Back End")
    assert info.branch == "cl/back-end/7"
    assert Path(info.path) == (root / "my-repo" / "7").resolve()
    assert not info.dirty and info.ahead == 0 and info.files_vs_base == []
    assert (Path(info.path) / "app.py").read_text() == "x = 1\n"
    # Main checkout untouched: still on main, clean.
    assert g(r, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert g(r, "status", "--porcelain") == ""
    assert W.list_session_worktrees(r) == [{"path": info.path, "branch": "cl/back-end/7",
                                            "head": info.head}]


def test_inspect_commit_then_prune_keeps_work(repo):
    r, _ = repo
    info = W.create(r, "main", session_id=1, role="backend")
    wt = Path(info.path)
    (wt / "app.py").write_text("x = 2\n")
    (wt / "new.txt").write_text("n\n")
    i = W.inspect(r, wt, "main")
    assert i.dirty and set(i.changed) == {"app.py", "new.txt"}
    res = W.prune(r, wt, "main")
    assert not res.removed and "uncommitted" in res.reason and wt.exists()
    commit_all(wt, "work")
    i = W.inspect(r, wt, "main")
    assert not i.dirty and i.ahead == 1 and sorted(i.files_vs_base) == ["app.py", "new.txt"]
    res = W.prune(r, wt, "main")
    assert not res.removed and "1 commit" in res.reason
    assert wt.exists() and "cl/backend/1" in g(r, "branch", "--list", "cl/*")


def test_prune_clean_removes_worktree_and_branch(repo):
    r, _ = repo
    info = W.create(r, "main", session_id=2, role="docs")
    res = W.prune(r, info.path, "main")
    assert res.removed and not Path(info.path).exists()
    assert g(r, "branch", "--list", "cl/*") == ""
    assert W.list_session_worktrees(r) == []


def test_dry_merge_clean_and_conflict(repo):
    r, _ = repo
    a = W.create(r, "main", session_id=3, role="a")
    (Path(a.path) / "README.md").write_text("hello from a\n")
    commit_all(a.path, "a")
    res = W.dry_merge(r, "main", a.branch)
    assert res.clean and res.conflicts == [] and res.tree
    # Base moves on in the main checkout and touches the same line.
    (r / "README.md").write_text("hello from main\n")
    commit_all(r, "main change")
    head_before = g(r, "rev-parse", "HEAD")
    res = W.dry_merge(r, "main", a.branch)
    assert not res.clean and res.conflicts == ["README.md"]
    # Nothing was touched: same HEAD, clean checkouts.
    assert g(r, "rev-parse", "HEAD") == head_before
    assert g(r, "status", "--porcelain") == "" and g(a.path, "status", "--porcelain") == ""


def test_refusals(repo, tmp_path):
    r, root = repo
    W.create(r, "main", session_id=4, role="x")
    with pytest.raises(W.WorktreeError, match="already exists"):
        W.create(r, "main", session_id=4, role="x")
    with pytest.raises(W.WorktreeError, match="base_ref not found"):
        W.create(r, "nope", session_id=5, role="x")
    with pytest.raises(W.WorktreeError):
        W.create(r, "--orphan", session_id=5, role="x")
    with pytest.raises(W.WorktreeError, match="outside the repo"):
        W.create(r, "main", session_id=6, role="x", root=r / ".worktrees")
    with pytest.raises(W.WorktreeError, match="main checkout|outside the worktrees root"):
        W.inspect(r, r, "main")
    with pytest.raises(W.WorktreeError, match="main checkout"):
        W.prune(r, r, "main", root=r.parent)
    stray = root / "my-repo" / "99"
    stray.mkdir(parents=True)
    with pytest.raises(W.WorktreeError, match="not a registered worktree"):
        W.prune(r, stray, "main")
    for bad in ("main", "master", "feature/x"):
        with pytest.raises(W.WorktreeError):
            W._check_branch(r, bad, "main")


def test_foreign_worktree_on_main_branch_is_refused(repo):
    r, root = repo
    g(r, "branch", "other")
    foreign = root / "my-repo" / "50"
    foreign.parent.mkdir(parents=True)
    g(r, "worktree", "add", str(foreign), "other")
    with pytest.raises(W.WorktreeError, match="not a session branch"):
        W.prune(r, foreign, "main")
    assert foreign.exists()


def test_no_push_fetch_or_remote_commands():
    for sub in ("push", "fetch", "pull", "remote", "clone", "submodule", "config", "commit", "reset"):
        with pytest.raises(W.WorktreeError, match="not allowed"):
            W._git(".", sub)


def test_repo_hooks_never_run(repo):
    r, _ = repo
    hook = r / ".git" / "hooks" / "post-checkout"
    marker = r.parent / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    W.create(r, "main", session_id=8, role="x")
    assert not marker.exists()


def test_git_env_drops_secrets_and_redirects(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key-000000000000")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    env = W._env()
    assert not {"OPENAI_API_KEY", "GITHUB_TOKEN", "GIT_DIR", "GIT_SSH_COMMAND"} & set(env)
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_version_supports_merge_tree():
    assert W.git_version() >= W.MIN_MERGE_TREE_GIT


def test_prune_keeps_a_branch_that_moved_after_inspection(repo, monkeypatch):
    r, _ = repo
    info = W.create(r, "main", session_id=9, role="race")
    real_inspect = W.inspect

    def inspect_then_commit(*a, **k):
        snap = real_inspect(*a, **k)               # clean, ahead == 0…
        (Path(info.path) / "late.txt").write_text("late\n")   # …then the session commits
        commit_all(info.path, "late")
        return snap
    monkeypatch.setattr(W, "inspect", inspect_then_commit)
    res = W.prune(r, info.path, "main")
    assert not res.removed and "moved" in res.reason
    assert info.branch in g(r, "branch", "--list", "cl/*")
