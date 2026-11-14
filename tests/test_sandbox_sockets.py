"""bwrap must not leave filesystem Unix sockets reachable.

``--ro-bind / /`` is recursive and ``--unshare-net`` only hides abstract
sockets, so before this change a command inside the sandbox could still
connect to the D-Bus session bus, the X11 server, ssh-agent… (found with the
probe in the last test). The unit tests run anywhere; the real-bwrap test runs
where bwrap works (CI installs it and sets CONDUCTLOOP_REQUIRE_SANDBOX=1).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from ops_dashboard.sessions import sandbox

BACKEND = sandbox.detect_backend()


@pytest.fixture()
def base():
    # AF_UNIX paths are limited to ~108 bytes: keep them short, under /tmp.
    d = Path(tempfile.mkdtemp(prefix="sp-", dir="/tmp")).resolve()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _sock(path: Path, keep: list | None = None) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(path))
    s.listen(8)
    threading.Thread(target=lambda: [s.accept() for _ in range(20)], daemon=True).start()
    if keep is not None:
        keep.append(s)
    return s


@pytest.fixture()
def dirs(base):
    wt, tmp = base / "wt", base / "run"
    wt.mkdir()
    tmp.mkdir()
    return wt, tmp


def _argv_pairs(argv, flag):
    return [argv[i + 1:i + 3] for i, a in enumerate(argv) if a == flag]


# ── unit: what gets hidden ────────────────────────────────────────────────

def test_runtime_dir_and_x11_get_a_tmpfs(base, dirs, monkeypatch):
    wt, tmp = dirs
    xdg = base / "xdg"
    xdg.mkdir()
    monkeypatch.setattr(sandbox, "_SOCKET_DIRS", (str(base / "x11"),))
    (base / "x11").mkdir()
    d, _ = sandbox.socket_hides(worktree=wt, tmp=tmp, environ={"XDG_RUNTIME_DIR": str(xdg)})
    assert str(xdg) in d and str(base / "x11") in d


def test_env_pointed_sockets_are_covered(base, dirs, monkeypatch):
    wt, tmp = dirs
    monkeypatch.setattr(sandbox, "_SOCKET_SCAN_ROOTS", ())
    keep: list = []
    bus, agent, gpg = base / "a" / "bus", base / "b" / "agent", base / "c" / "S.gpg-agent"
    for p in (bus, agent, gpg):
        _sock(p, keep)
    env = {"DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus},guid=abc;unix:abstract=/x",
           "SSH_AUTH_SOCK": str(agent), "GPG_AGENT_INFO": f"{gpg}:1234:1"}
    _, files = sandbox.socket_hides(worktree=wt, tmp=tmp, environ=env)
    assert set(files) == {str(bus), str(agent), str(gpg)}


def test_scan_finds_sockets_but_never_the_worktree_or_run_tmp(base, dirs, monkeypatch):
    wt, tmp = dirs
    monkeypatch.setattr(sandbox, "_SOCKET_SCAN_ROOTS", (str(base),))
    keep: list = []
    _sock(base / "other" / "s.sock", keep)
    _sock(wt / "mine.sock", keep)
    _sock(tmp / "run.sock", keep)
    (base / "link").symlink_to(base / "other")
    _, files = sandbox.socket_hides(worktree=wt, tmp=tmp, environ={})
    assert files == [str(base / "other" / "s.sock")]


def test_a_dangerous_runtime_dir_is_not_replaced_wholesale(dirs, monkeypatch):
    wt, tmp = dirs
    for bad in ("/", "/tmp", "/run", "/usr", "/etc", "/home", ""):
        d, _ = sandbox.socket_hides(worktree=wt, tmp=tmp, environ={"XDG_RUNTIME_DIR": bad})
        assert bad not in d or bad == ""


def test_bwrap_argv_covers_them_before_binding_the_worktree(base, dirs, monkeypatch):
    wt, tmp = dirs
    xdg = base / "xdg"
    xdg.mkdir()
    keep: list = []
    agent = base / "ag" / "agent"
    _sock(agent, keep)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(agent))
    argv = sandbox.bwrap_argv(["true"], worktree=wt, tmp=tmp, cwd=wt)
    assert str(xdg) in [p[0] for p in _argv_pairs(argv, "--tmpfs")]
    assert [os.devnull, str(agent)] in _argv_pairs(argv, "--ro-bind")
    bind_wt = argv.index("--bind")
    assert argv.index(str(xdg)) < bind_wt and argv.index(str(agent)) < bind_wt
    assert argv[argv.index("--"):] == ["--", "true"]
    assert "--unshare-net" in argv and "--die-with-parent" in argv


def test_no_bwrap_means_no_execution(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("CONDUCTLOOP_SANDBOX", raising=False)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    monkeypatch.setattr(sandbox, "_PROBED", {})
    assert sandbox.detect_backend() is None
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.run(["true"], worktree=tmp_path, cwd=tmp_path, path_dirs=[])


def test_macos_profile_does_not_depend_on_the_linux_socket_scan(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("macOS sandbox must not use the bwrap socket logic")
    monkeypatch.setattr(sandbox, "socket_hides", boom)
    prof = sandbox.seatbelt_profile(worktree=tmp_path, tmp=tmp_path)
    assert "(deny network*)" in prof


# ── real bwrap ───────────────────────────────────────────────────────────

needs_bwrap = pytest.mark.skipif(
    BACKEND != "bwrap" and not os.environ.get("CONDUCTLOOP_REQUIRE_SANDBOX"),
    reason="bwrap is not available here")

CONNECT = ("import socket,sys\n"
           "s=socket.socket(socket.AF_UNIX); s.settimeout(5)\n"
           "try:\n s.connect(sys.argv[1]); print('REACHED')\n"
           "except OSError as e: print('blocked', type(e).__name__)\n")


def _connect(path, *, sandboxed, wt, tmp):
    cmd = [sys.executable, "-c", CONNECT, str(path)]
    if sandboxed:
        cmd = sandbox.bwrap_argv(cmd, worktree=wt, tmp=tmp, cwd=wt)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin"})
    return r.stdout.strip(), r


@needs_bwrap
def test_sockets_are_unreachable_inside_bwrap_and_reachable_outside(base, dirs, monkeypatch):
    """The same probe as the manual check: connect() from inside the sandbox."""
    wt, tmp = dirs
    keep: list = []
    xdg = base / "xdg"
    bus = xdg / "bus"                       # $XDG_RUNTIME_DIR/bus (D-Bus session bus)
    agent = base / "agent-dir" / "agent.1"  # $SSH_AUTH_SOCK
    scanned = base.parent / f"{base.name}-x" / "s.sock"   # /tmp/<dir>/<sock>, found by the scan
    for p in (bus, agent, scanned):
        _sock(p, keep)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={bus}")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(agent))
    try:
        for p in (bus, agent, scanned):
            out, r = _connect(p, sandboxed=False, wt=wt, tmp=tmp)
            assert out == "REACHED", (p, r.stderr)               # control: it is a live socket
            out, r = _connect(p, sandboxed=True, wt=wt, tmp=tmp)
            assert out.startswith("blocked"), (p, out, r.stderr)  # and bwrap now hides it
        # the sandbox itself still works
        r = subprocess.run(sandbox.bwrap_argv(["echo", "ok"], worktree=wt, tmp=tmp, cwd=wt),
                           capture_output=True, text=True, timeout=60)
        assert r.stdout.strip() == "ok"
    finally:
        shutil.rmtree(scanned.parent, ignore_errors=True)


@needs_bwrap
def test_run_still_works_and_cannot_reach_the_agent_socket(base, dirs, monkeypatch):
    wt, tmp = dirs
    keep: list = []
    agent = base / "ag" / "agent"
    _sock(agent, keep)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(agent))
    res = sandbox.run([sys.executable, "-c", CONNECT, str(agent)], worktree=wt, cwd=wt,
                      path_dirs=[str(Path(sys.executable).parent)], backend="bwrap")
    assert res.exit_code == 0 and res.output.strip().startswith("blocked"), res
