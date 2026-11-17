"""The tools the agent works with.

Four things the exam showed were missing:

* ``read_file`` returned the text without the numbers of the lines, so the
  model could not point at a place in a file, nor build an edit on it;
* ``edit_file`` said only "found 0 occurrences", so the model had to read
  the file again to find out what it had got wrong;
* a change that touched several places needed one call per place, and one
  turn each;
* the whitelist of ``run`` refused ``python <file>``, ``python -m <module>``
  and the read-only git commands, and the agent gave up on them.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from ops_dashboard.sessions import SessionStore, sandbox
from ops_dashboard.sessions import worktrees as W
from ops_dashboard.sessions.code_tools import CodeTools, ToolDenied, ToolError

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}
BACKEND = sandbox.detect_backend()


def g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV, "HOME": str(cwd)}).stdout


@pytest.fixture()
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "s.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db

    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    g(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\ny = 2\nz = 3\n")
    (repo / "script.py").write_text("print('hello')\n")
    g(repo, "add", ".")
    g(repo, "commit", "-q", "-m", "init")
    monkeypatch.setenv(W.WORKTREES_ENV, str(tmp_path / "wt-root"))
    store = SessionStore()
    store.mark_repo(str(repo))

    def make(name: str) -> CodeTools:
        sid = store.create_session(name=name, role=name, provider="openai", model="m",
                                   repo_path=str(repo), base_ref="main", budget_cost_cents=100)
        info = W.create(repo, "main", session_id=sid, role=name)
        store.set_session_worktree(sid, info.path, info.branch)
        tid = store.enqueue_task(title=f"t-{name}", prompt="p", assigned_session_id=sid)
        assert store.claim_task(sid, tid)
        return CodeTools(store, sid, tid, python=sys.executable, timeout_s=60,
                         sandbox_backend=BACKEND, allow_unsandboxed=BACKEND is None)

    return {"store": store, "repo": repo, "make": make}


# ── read_file ──────────────────────────────────────────────────────────────

def test_read_file_numbers_the_lines(env):
    t = env["make"]("a")
    out = t.read_file("src/app.py")
    assert out["text"] == "1: x = 1\n2: y = 2\n3: z = 3\n"
    assert out["start_line"] == 1


def test_read_file_reads_a_range_of_lines(env):
    t = env["make"]("a")
    out = t.read_file("src/app.py", start_line=2, end_line=3)
    assert out["text"] == "2: y = 2\n3: z = 3\n"
    assert out["start_line"] == 2 and out["end_line"] == 3
    assert out["total_lines"] == 3


def test_read_file_of_a_range_beyond_the_file_is_not_an_error(env):
    t = env["make"]("a")
    out = t.read_file("src/app.py", start_line=2, end_line=99)
    assert out["text"] == "2: y = 2\n3: z = 3\n"
    # Everything after the second line was returned: nothing is left.
    assert out["truncated"] is False
    # A range that stops before the end says so.
    assert t.read_file("src/app.py", start_line=1, end_line=2)["truncated"] is True


# ── edit_file ──────────────────────────────────────────────────────────────

def test_edit_file_says_which_lines_look_like_the_old_text(env):
    t = env["make"]("a")
    t.claim(["src/app.py"])
    with pytest.raises(ToolError) as ei:
        t.edit_file("src/app.py", "x = 42\n", "x = 43\n")
    message = str(ei.value)
    assert "found 0" in message
    # The line that looks like what it was looking for, with its number.
    assert "1: x = 1" in message


def test_edit_file_still_says_how_many_it_found(env):
    t = env["make"]("a")
    t.claim(["src/app.py"])
    with pytest.raises(ToolError) as ei:
        t.edit_file("src/app.py", "nope", "x")
    assert "found 0" in str(ei.value)


# ── patch ──────────────────────────────────────────────────────────────────

PATCH = """--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,3 @@
 x = 1
-y = 2
+y = 20
 z = 3
"""


def test_patch_applies_a_unified_diff(env):
    t = env["make"]("a")
    t.claim(["src/app.py"])
    out = t.apply_patch(PATCH)
    assert out["paths"] == ["src/app.py"]
    assert (t.root / "src" / "app.py").read_text() == "x = 1\ny = 20\nz = 3\n"


def test_patch_needs_a_reservation(env):
    t = env["make"]("a")
    with pytest.raises(ToolDenied, match="reservation"):
        t.apply_patch(PATCH)


def test_patch_that_does_not_apply_says_so(env):
    t = env["make"]("a")
    t.claim(["src/app.py"])
    broken = PATCH.replace("@@ -1,3 +1,3 @@", "@@ -9,9 +9,9 @@")
    with pytest.raises(ToolError):
        t.apply_patch(broken)


def test_patch_of_a_file_outside_the_worktree_is_refused(env):
    t = env["make"]("a")
    t.claim(["**"])
    outside = "--- a/../outside.txt\n+++ b/../outside.txt\n@@ -1 +1 @@\n-a\n+b\n"
    with pytest.raises(ToolDenied):
        t.apply_patch(outside)


# ── run: python ───────────────────────────────────────────────────────────

def test_python_runs_a_file_of_the_repository(env):
    t = env["make"]("a")
    full, _ = t.build_command(["python", "script.py"])
    assert full[1:] == ["script.py"]
    # The interpreter of the session, whatever it is called: python, python3,
    # python3.14... (the venv of this machine answers to the last one).
    assert os.path.basename(full[0]).startswith("python"), full[0]


def test_python_runs_a_module(env):
    t = env["make"]("a")
    full, _ = t.build_command(["python", "-m", "pytest", "-q", "src/app.py"])
    # It is the pytest of the runner: the same options, the same root.
    assert full[1:3] == ["-m", "pytest"]
    assert "src/app.py" in full


def test_python_runs_any_other_module(env):
    t = env["make"]("a")
    full, _ = t.build_command(["python", "-m", "json.tool"])
    assert full[1:3] == ["-m", "json.tool"]


def test_python_without_a_file_or_a_module_is_refused(env):
    t = env["make"]("a")
    with pytest.raises(ToolDenied):
        t.build_command(["python"])
    with pytest.raises(ToolDenied):
        t.build_command(["python", "-m"])


def test_python_cannot_run_code_from_the_command_line(env):
    t = env["make"]("a")
    with pytest.raises(ToolDenied):
        t.build_command(["python", "-c", "print(1)"])


def test_python_only_runs_a_file_of_the_worktree(env):
    t = env["make"]("a")
    with pytest.raises(ToolDenied):
        t.build_command(["python", "../outside.py"])


# ── run: git ──────────────────────────────────────────────────────────────

def test_the_read_only_git_commands_are_allowed(env):
    t = env["make"]("a")
    for sub in ("log", "show", "blame", "diff"):
        full, _ = t.build_command(["git", sub])
        assert "git" in full[0] and sub in full
        # The flags that make a git command safe are always there.
        assert any("core.hooksPath=" in a for a in full)
        assert any("protocol.allow=never" in a for a in full)


def test_git_log_takes_its_flags(env):
    t = env["make"]("a")
    full, _ = t.build_command(["git", "log", "--oneline", "-n", "5"])
    assert "--oneline" in full and "5" in full


def test_git_paths_after_the_dashes_must_be_in_the_worktree(env):
    t = env["make"]("a")
    full, _ = t.build_command(["git", "diff", "--", "src/app.py"])
    assert "src/app.py" in full
    with pytest.raises(ToolDenied):
        t.build_command(["git", "diff", "--", "../outside.txt"])


def test_git_that_writes_is_refused(env):
    t = env["make"]("a")
    for argv in (["git", "commit", "-m", "x"], ["git", "checkout", "main"],
                 ["git", "push"], ["git", "reset", "--hard"], ["git", "stash"]):
        with pytest.raises(ToolDenied):
            t.build_command(argv)


def test_git_flags_that_write_or_run_are_refused(env):
    t = env["make"]("a")
    for argv in (["git", "log", "--output=out.txt"], ["git", "diff", "--ext-diff"],
                 ["git", "log", "-c", "core.pager=cat"], ["git", "diff", "--output=/tmp/x"]):
        with pytest.raises(ToolDenied):
            t.build_command(argv)


def test_nothing_else_is_allowed(env):
    t = env["make"]("a")
    for argv in (["pip", "install", "requests"], ["curl", "https://example.com"],
                 ["rm", "-rf", "/"], ["bash", "-c", "ls"]):
        with pytest.raises(ToolDenied):
            t.build_command(argv)
