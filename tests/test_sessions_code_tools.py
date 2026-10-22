"""sessions.code_tools / sessions.sandbox: escape attempts against the
confinement, D3 write rules, the command whitelist and the runner limits.

Throwaway git repo + DB per test; no network needed, no real keys (the "keys"
below are fake strings planted in the environment to prove they don't leak).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ops_dashboard.sessions import SessionStore, sandbox
from ops_dashboard.sessions import code_tools as CT
from ops_dashboard.sessions import worktrees as W
from ops_dashboard.sessions.code_tools import CodeTools, ToolDenied, ToolError
from ops_dashboard.sessions.paths import PathConflictError

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}
FAKE_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE0123456789"
BACKEND = sandbox.detect_backend()
# CI installs bubblewrap and sets CONDUCTLOOP_REQUIRE_SANDBOX=1: there the
# isolation test must run (it fails, never skips, if the sandbox is missing).
needs_sandbox = pytest.mark.skipif(
    BACKEND is None and not os.environ.get("CONDUCTLOOP_REQUIRE_SANDBOX"),
    reason="no OS sandbox (bwrap/sandbox-exec) here")


def g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV, "HOME": str(cwd)}).stdout


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


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
    (repo / "src" / "app.py").write_text("x = 1\n")
    (repo / "src" / "util.js").write_text("export const y = 2;\n")
    (repo / "server.py").write_text("# hot\n")
    (repo / "README.md").write_text("hello world\n")
    g(repo, "add", ".")
    g(repo, "commit", "-q", "-m", "init")
    root = tmp_path / "wt-root"
    monkeypatch.setenv(W.WORKTREES_ENV, str(root))
    clock = Clock()
    store = SessionStore(clock=clock)
    store.mark_repo(str(repo))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP-SECRET\n")

    def make(name: str) -> CodeTools:
        sid = store.create_session(name=name, role=name, provider="openai", model="m",
                                   repo_path=str(repo), base_ref="main", budget_cost_cents=100)
        info = W.create(repo, "main", session_id=sid, role=name)
        store.set_session_worktree(sid, info.path, info.branch)
        tid = store.enqueue_task(title=f"t-{name}", prompt="p", assigned_session_id=sid)
        assert store.claim_task(sid, tid)
        return CodeTools(store, sid, tid, python=sys.executable, timeout_s=60,
                         sandbox_backend=BACKEND, allow_unsandboxed=BACKEND is None)

    return {"store": store, "repo": repo, "make": make, "outside": outside, "clock": clock,
            "tmp": tmp_path}


def _events(t: CodeTools) -> list[dict]:
    return t.store.list_events(t.session_id)


# ── path confinement ───────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["../outside/secret.txt", "src/../../outside/secret.txt",
                                 "/etc/passwd", "~/.ssh/id_rsa", "src\\..\\..\\x", "a\0b",
                                 "C:/Windows", ".git/config", ".GIT/config", "src/.git",
                                 ".g\u200cit/HEAD", ".git./config", "x/.Git/hooks/pre-commit"])
def test_traversal_absolute_and_git_are_refused(env, bad):
    t = env["make"]("a")
    t.claim(["**"])
    for call in (lambda: t.read_file(bad), lambda: t.list_dir(bad),
                 lambda: t.search("x", bad), lambda: t.write_file(bad, "pwn")):
        with pytest.raises(ToolDenied):
            call()
    assert (env["outside"] / "secret.txt").read_text() == "TOP-SECRET\n"


def test_symlink_pointing_outside_is_refused_for_read_and_write(env):
    t = env["make"]("a")
    t.claim(["**"])
    (t.root / "leak").symlink_to(env["outside"] / "secret.txt")
    (t.root / "leakdir").symlink_to(env["outside"], target_is_directory=True)
    for p in ("leak", "leakdir/secret.txt", "leakdir"):
        with pytest.raises(ToolDenied, match="outside"):
            t.read_file(p)
        with pytest.raises(ToolDenied):
            t.write_file(p, "pwn")
    with pytest.raises(ToolDenied):
        t.list_dir("leakdir")
    assert (env["outside"] / "secret.txt").read_text() == "TOP-SECRET\n"
    # listing shows the link as a link, never follows it
    names = {e["path"]: e["type"] for e in t.list_dir(".")["entries"]}
    assert names["leak"] == "symlink" and ".git" not in names


def test_internal_symlinks_are_readable_but_never_written_through(env):
    t = env["make"]("a")
    t.claim(["**"])
    (t.root / "alias.py").symlink_to("src/app.py")
    (t.root / "srcalias").symlink_to("src", target_is_directory=True)
    assert t.read_file("alias.py")["text"] == "x = 1\n"
    with pytest.raises(ToolDenied, match="symlink"):
        t.write_file("alias.py", "y = 2\n")
    with pytest.raises(ToolDenied, match="symlink"):
        t.write_file("srcalias/app.py", "y = 2\n")
    # a symlink to .git is refused even though it stays inside the worktree
    (t.root / "g").symlink_to(".git")
    with pytest.raises(ToolDenied):
        t.read_file("g")
    assert (t.root / "src" / "app.py").read_text() == "x = 1\n"


# ── D3: reservations at write time ─────────────────────────────────────────

def test_write_requires_an_active_reservation(env):
    t = env["make"]("a")
    with pytest.raises(ToolDenied, match="reservation"):
        t.write_file("src/app.py", "x = 2\n")
    t.claim(["src/app.py"], ttl_seconds=60)
    assert t.write_file("src/app.py", "x = 2\n")["bytes"] == 6
    assert (t.root / "src/app.py").read_text() == "x = 2\n"
    with pytest.raises(ToolDenied):                      # not covered by the claim
        t.write_file("src/other.py", "")
    env["clock"].t += 61                                 # reservation expired
    with pytest.raises(ToolDenied, match="reservation"):
        t.edit_file("src/app.py", "2", "3")


def test_write_refused_when_task_not_in_flight(env):
    t = env["make"]("a")
    t.claim(["src/app.py"])
    assert env["store"].set_task_status(t.task_id, t.session_id, "done")
    with pytest.raises(ToolDenied, match="task"):
        t.write_file("src/app.py", "x = 9\n")


def test_reservation_of_another_session_blocks_and_precise_overlap(env):
    a, b = env["make"]("a"), env["make"]("b")
    a.claim(["src/*.py"])
    # Conservative #18 check would clash ("src/" prefix); git ls-files says no
    # existing file matches both, so the precise check grants it.
    b.claim(["src/*.js"])
    # A real overlap on an existing file is still a conflict.
    with pytest.raises(PathConflictError):
        b.claim(["src/app.py"])
    # Glob-vs-glob on a file that doesn't exist yet: both claims are granted…
    b.claim(["src/new*"])
    # …but the per-path write check refuses the file both patterns cover.
    with pytest.raises(ToolDenied, match="session #"):
        a.write_file("src/new_mod.py", "z = 1\n")
    with pytest.raises(ToolDenied, match="session #"):
        b.write_file("src/new_mod.py", "z = 1\n")
    a.write_file("src/app.py", "x = 5\n")               # only A's
    b.write_file("src/util.js", "export const y = 3;\n")  # only B's
    with pytest.raises(ToolDenied):
        b.write_file("src/app.py", "x = 6\n")             # A's reservation, not B's
    assert not (a.root / "src/new_mod.py").exists()


def test_exclusive_files(env):
    a, b = env["make"]("a"), env["make"]("b")
    granted = a.claim(["server.py"])
    assert granted[0]["mode"] == "exclusive"
    with pytest.raises(PathConflictError):
        b.claim(["server.py"])
    with pytest.raises(PathConflictError):
        b.claim(["*.py"])                               # can match server.py
    a.write_file("server.py", "# edited\n")
    with pytest.raises(ToolDenied):
        b.write_file("server.py", "# pwn\n")
    assert (a.root / "server.py").read_text() == "# edited\n"


def test_edit_and_diff(env):
    t = env["make"]("a")
    t.claim(["src/**"])
    with pytest.raises(ToolError, match="occurrence"):
        t.edit_file("src/app.py", "nope", "x")
    t.edit_file("src/app.py", "x = 1", "x = 42")
    t.write_file("src/pkg/new.py", "print('hi')\n")      # creates the directory
    d = t.diff()
    assert "+x = 42" in d["diff"]
    assert {"status": "??", "path": "src/pkg/new.py"} in d["changed"]
    assert t.diff(against="base")["diff"]
    with pytest.raises(ToolDenied):
        t.diff(against="--output=/tmp/x")


def test_write_size_limit_and_atomicity(env):
    t = env["make"]("a")
    t.claim(["big.txt"])
    with pytest.raises(ToolDenied, match="exceeds"):
        t.write_file("big.txt", "x" * (CT.MAX_WRITE_BYTES + 1))
    assert not (t.root / "big.txt").exists()
    t.write_file("big.txt", "ok")
    assert not [p for p in t.root.iterdir() if p.name.startswith(".cl-tmp-")]


def test_search_is_literal_and_option_safe(env):
    t = env["make"]("a")
    (t.root / "notes.txt").write_text("--output=/tmp/pwn\nfoo(bar)\n")  # untracked too
    r = t.search("--output=/tmp/pwn")
    assert r["matches"][0]["path"] == "notes.txt"
    assert t.search("foo(bar)")["matches"]                 # literal, not regex
    assert t.search("hello", "README.md")["matches"][0]["line"] == 1
    assert not Path("/tmp/pwn").exists()


def test_worktree_must_be_a_registered_session_worktree(env):
    store, repo = env["store"], env["repo"]
    sid = store.create_session(name="x", role="x", provider="openai", model="m",
                               repo_path=str(repo), base_ref="main", budget_cost_cents=100)
    tid = store.enqueue_task(title="t", prompt="p", assigned_session_id=sid)
    store.claim_task(sid, tid)
    with pytest.raises(ToolDenied, match="no worktree"):
        CodeTools(store, sid, tid)
    store.set_session_worktree(sid, str(repo), "main")   # the main checkout
    with pytest.raises(W.WorktreeError):
        CodeTools(store, sid, tid)


# ── command whitelist ──────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ["bash", "-c", "id"], ["sh", "-c", "id"], ["python", "-c", "print(1)"],
    ["python", "-m", "pip", "install", "x"], ["pip", "install", "requests"],
    ["npm", "install"], ["npm", "ci"], ["npm", "run", "build"], ["npm", "test", "--", "x"],
    ["npx", "jest"], ["git", "push"], ["curl", "http://example.com"],
    ["ruff", "check", "--fix"], ["ruff", "check", "--unsafe-fixes"], ["ruff", "format"],
    ["ruff", "check", "--config", "/tmp/x.toml"], ["ruff", "clean"],
    ["mypy", "--install-types"], ["mypy", "--config-file", "/etc/x"],
    ["pytest", "-p", "evil_plugin"], ["pytest", "--rootdir=/"], ["pytest", "-c", "/etc/x.ini"],
    ["pytest", "-o", "addopts=-p evil"], ["pytest", "--basetemp=/"], ["pytest", "--pdb"],
    ["pytest", "@args.txt"], ["pytest", "../outside"], ["pytest", "/etc"],
    ["pytest", "-k", "x; rm -rf /"], ["pytest", "-k", "$(id)"], ["pytest", "-k", "`id`"],
    ["pytest", "src/app.py; touch PWNED"], ["pytest", "src/app.py\nid"],
    ["pytest", "--maxfail", "1; id"], ["pytest", "--tb", "evil"], ["pytest", "-k"],
    ["pytest", ".git"], ["pytest", "src/app.py::x|id"], [], "pytest -q", ["pytest", ""],
])
def test_commands_outside_the_whitelist_or_with_bad_args_are_refused(env, argv):
    t = env["make"]("a")
    with pytest.raises((ToolDenied, ToolError)):
        t.run(argv)
    assert not (t.root / "PWNED").exists()
    assert _events(t)[-1]["kind"] == "tool.run"
    assert json.loads(_events(t)[-1]["payload"])["ok"] is False


def test_whitelisted_commands_build_as_argv_lists(env):
    t = env["make"]("a")
    full, _ = t.build_command(["pytest", "-q", "-k", "not slow", "src/app.py::test_x[1]"])
    assert full[1:6] == ["-m", "pytest", "-p", "no:cacheprovider", "--rootdir"]
    assert full[-3:] == ["-k", "not slow", "src/app.py::test_x[1]"]
    assert t.build_command(["ruff", "check", "--select", "E,F", "src"])[0][-4:] == \
        ["--no-cache", "--select", "E,F", "src"][-4:]
    assert t.build_command(["python", "-m", "mypy", "--strict", "src"])[0][1:3] == ["-m", "mypy"]
    assert t.build_command(["ruff", "format", "--check"])[0][3] == "format"


# ── runner: env, timeout, output, file size, events ────────────────────────

def _test_file(t: CodeTools, name: str, body: str) -> None:
    (t.root / name).write_text(textwrap.dedent(body))


def test_run_env_has_no_keys_proxies_or_real_home(env, monkeypatch):
    for k, v in {"OPENAI_API_KEY": FAKE_KEY, "ANTHROPIC_API_KEY": FAKE_KEY,
                 "GITHUB_TOKEN": "ghp_" + "A" * 36, "AWS_SECRET_ACCESS_KEY": "x" * 40,
                 "HTTPS_PROXY": "http://proxy:3128", "http_proxy": "http://proxy:3128",
                 "PYTEST_ADDOPTS": "-p evil", "GIT_ASKPASS": "/bin/echo",
                 "CONDUCTLOOP_SECRET": "s3cr3t"}.items():
        monkeypatch.setenv(k, v)
    t = env["make"]("a")
    _test_file(t, "test_env.py", f"""
        import os
        def test_env():
            bad = [k for k in os.environ if any(s in k.upper() for s in
                   ("KEY", "TOKEN", "SECRET", "PROXY", "ASKPASS", "PYTEST_ADDOPTS"))
                   and k not in ("no_proxy", "NO_PROXY")]
            assert bad == []
            assert {FAKE_KEY!r} not in repr(dict(os.environ))
            assert os.environ["HOME"] != {os.path.expanduser("~")!r}
            assert os.environ["no_proxy"] == "*"
    """)
    r = t.run(["pytest", "-q", "test_env.py"])
    assert r["exit_code"] == 0, r["output"]
    assert "1 passed" in r["output"]


def test_run_timeout_kills_the_process_group(env):
    t = env["make"]("a")
    t.timeout_s = 2
    _test_file(t, "test_slow.py", """
        import subprocess, time
        def test_slow():
            subprocess.Popen(["sleep", "120"])
            time.sleep(120)
    """)
    r = t.run(["pytest", "-q", "test_slow.py"])
    assert r["timed_out"] is True and r["exit_code"] is None
    assert r["duration_ms"] < 30_000
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    assert "sleep 120" not in out


def test_run_huge_output_is_capped(env):
    t = env["make"]("a")
    t.max_output = 8192
    _test_file(t, "test_loud.py", """
        import sys
        def test_loud():
            for _ in range(50_000):
                sys.stdout.write("A" * 99 + "\\n")
            sys.stdout.write("THE-END\\n")
    """)
    r = t.run(["pytest", "-q", "-s", "test_loud.py"])
    assert r["exit_code"] == 0
    assert r["output_bytes"] > 5_000_000 and r["truncated"] is True
    assert len(r["output"]) < 8192 + 200
    assert "passed" in r["output"]                     # the tail is kept


def test_run_file_size_limit(env):
    t = env["make"]("a")
    t.max_file_bytes = 1024 * 1024
    _test_file(t, "test_fill.py", """
        def test_fill():
            with open("fill.bin", "wb") as f:
                for _ in range(8):
                    f.write(b"0" * (512 * 1024))
    """)
    t.claim(["fill.bin"])        # reserved, so the partial file is kept for the check
    r = t.run(["pytest", "-q", "test_fill.py"])
    assert r["exit_code"] != 0
    assert (t.root / "fill.bin").stat().st_size <= 1024 * 1024


def test_run_cannot_write_outside_the_task_reservations(env):
    t = env["make"]("a")
    (t.root / "src").mkdir(exist_ok=True)
    (t.root / "keep.txt").write_text("my edit\n")          # an uncommitted edit before the run
    t.claim(["src/ok.txt"])
    _test_file(t, "test_sneaky.py", """
        import pathlib
        def test_sneaky():
            pathlib.Path("src/ok.txt").write_text("allowed")
            pathlib.Path("keep.txt").write_text("overwritten")
            pathlib.Path("new_unreserved.py").write_text("x = 1")
    """)
    r = t.run(["pytest", "-q", "test_sneaky.py"])
    assert r["exit_code"] == 0, r["output"]
    assert r["changed_paths"] == ["src/ok.txt"]
    assert {"keep.txt", "new_unreserved.py"} <= set(r["reverted_unreserved"])   # (+ .pytest_cache)
    assert (t.root / "src/ok.txt").read_text() == "allowed"
    assert (t.root / "keep.txt").read_text() == "my edit\n"
    assert not (t.root / "new_unreserved.py").exists()


def test_args_are_never_shell_interpreted(env):
    t = env["make"]("a")
    name = "test_$(touch PWNED)_x.py"
    _test_file(t, name, "def test_ok():\n    pass\n")
    r = t.run(["pytest", "-q", name, "-k", "ok or (x and not y)"])
    assert r["exit_code"] == 0, r["output"]
    assert not (t.root / "PWNED").exists()


def test_events_are_logged_without_sensitive_content(env):
    t = env["make"]("a")
    t.claim(["secret_notes.txt"])
    t.write_file("secret_notes.txt", f"CONTENT-MARKER {FAKE_KEY}\n")
    t.read_file("secret_notes.txt")
    t.search("CONTENT-MARKER")
    _test_file(t, "test_out.py", "def test_o():\n    print('OUTPUT-MARKER')\n")
    t.run(["pytest", "-q", "-s", "test_out.py"])
    with pytest.raises(ToolDenied):
        t.read_file("../outside/secret.txt")
    evs = _events(t)
    kinds = [e["kind"] for e in evs]
    for k in ("tool.claim", "tool.write", "tool.read", "tool.search", "tool.run"):
        assert k in kinds
    blob = json.dumps(evs)
    for marker in ("CONTENT-MARKER", "OUTPUT-MARKER", FAKE_KEY, "TOP-SECRET"):
        assert marker not in blob
    last = json.loads(evs[-1]["payload"])
    assert last["ok"] is False and last["denied"] is True


# ── sandbox ────────────────────────────────────────────────────────────────

def test_refuses_to_run_without_sandbox_unless_explicitly_allowed(env, monkeypatch):
    t = env["make"]("a")
    monkeypatch.setattr(sandbox, "detect_backend", lambda: None)
    t.sandbox_backend, t.allow_unsandboxed = None, False
    _test_file(t, "test_ok.py", "def test_ok():\n    pass\n")
    with pytest.raises(sandbox.SandboxUnavailable):
        t.run(["pytest", "-q", "test_ok.py"])
    assert json.loads(_events(t)[-1]["payload"])["denied"] is True
    t.allow_unsandboxed = True
    assert t.run(["pytest", "-q", "test_ok.py"])["sandbox"] == "none"


def test_sandbox_argv_and_profile_are_fail_closed():
    wt, tmp = Path("/w/t"), Path("/t/mp")
    argv = sandbox.bwrap_argv(["python", "-m", "pytest"], worktree=wt, tmp=tmp, cwd=wt)
    assert "--unshare-net" in argv and argv[argv.index("--ro-bind") + 1:][:2] == ["/", "/"]
    assert argv[-4:] == ["--", "python", "-m", "pytest"]
    prof = sandbox.seatbelt_profile(worktree=wt, tmp=tmp)
    assert "(deny network*)" in prof and '(subpath "/w/t")' in prof
    assert "(deny file-write* (require-not" in prof
    q = sandbox.seatbelt_profile(worktree=Path('/w/"x") (allow network*'), tmp=tmp)
    assert '(subpath "/w/\\"x\\") (allow network*")' in q   # quoting can't break out


@needs_sandbox
def test_sandbox_blocks_network_and_writes_outside(env):
    t = env["make"]("a")
    target = env["outside"] / "written.txt"
    _test_file(t, "test_escape.py", f"""
        import socket, pytest
        def test_no_network():
            with pytest.raises(OSError):
                socket.create_connection(("1.1.1.1", 53), timeout=3)
        def test_no_write_outside():
            with pytest.raises(OSError):
                open({str(target)!r}, "w").write("x")
        def test_no_read_ssh():
            import os
            p = os.path.join({os.path.expanduser("~")!r}, ".ssh")
            assert not os.path.isdir(p) or os.listdir(p) == []
    """)
    r = t.run(["pytest", "-q", "test_escape.py"])
    assert r["sandbox"] == BACKEND
    assert r["exit_code"] == 0, r["output"]
    assert not target.exists()


def test_tampered_gitlink_is_detected(env):
    """Code run by the agent could rewrite the worktree's `.git` file to aim
    our git calls at another repository; the pinned gitdir catches it."""
    t = env["make"]("a")
    (t.root / ".git").write_text(f"gitdir: {env['repo'] / '.git'}\n")
    with pytest.raises(ToolDenied, match="tampered"):
        t.diff()
    with pytest.raises(ToolDenied, match="tampered"):
        t.search("x")


def test_a_session_cannot_be_pointed_at_another_sessions_worktree(env):
    a, b = env["make"]("a"), env["make"]("b")
    store = env["store"]
    sb = store.get_session(b.session_id)
    store.set_session_worktree(a.session_id, sb["worktree_path"], sb["branch"])
    with pytest.raises(ToolDenied, match="does not belong"):
        CodeTools(store, a.session_id, a.task_id, python=sys.executable,
                  sandbox_backend=BACKEND, allow_unsandboxed=BACKEND is None)


def test_unknown_sandbox_backend_fails_closed(env):
    t = env["make"]("a")
    t.sandbox_backend = "bubblewrap"                    # a typo must never mean "no sandbox"
    with pytest.raises(sandbox.SandboxUnavailable):
        t.run(["pytest", "-q"])


def test_invalid_claims_are_audited(env):
    t = env["make"]("a")
    with pytest.raises(ToolDenied):
        t.claim(["../outside.txt"])
    ev = [e for e in _events(t) if e["kind"] == "tool.claim"]
    assert ev and json.loads(ev[-1]["payload"])["ok"] is False
