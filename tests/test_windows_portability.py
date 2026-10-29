"""Windows-safe behaviour of the Python side (PR 8 of docs/PORTABILITY.md).

Most tests run on every platform with ``sys.platform`` and a fake ``kernel32``
monkeypatched in, so the Windows branches are exercised on Linux and macOS CI.
The tests marked ``windows_only`` run only on a real Windows runner (the
``windows-latest`` job). UNTESTED on a real Windows machine beyond that job.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from ops_dashboard import fileperms, parent_watch, server

ROOT = Path(__file__).resolve().parent.parent
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs a real Windows")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits do not exist on Windows")


# ── parent_watch.alive: never os.kill on Windows ─────────────────────────────

class FakeKernel32:
    def __init__(self, *, handle=1234, wait=0x102):
        self.handle, self.wait = handle, wait
        self.opened: list[tuple[int, bool, int]] = []
        self.waited: list[tuple[int, int]] = []
        self.closed: list[int] = []

    def OpenProcess(self, access, inherit, pid):  # noqa: N802 - Win32 name
        self.opened.append((access, inherit, pid))
        return self.handle

    def WaitForSingleObject(self, handle, ms):  # noqa: N802
        self.waited.append((handle, ms))
        return self.wait

    def CloseHandle(self, handle):  # noqa: N802
        self.closed.append(handle)
        return 1


@pytest.fixture()
def no_kill(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("os.kill must never be called on Windows: it terminates the process")
    monkeypatch.setattr(os, "kill", boom)


def test_alive_on_windows_never_calls_os_kill(monkeypatch, no_kill):
    k = FakeKernel32()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(parent_watch, "_kernel32", lambda: k)
    assert parent_watch.alive(4242) is True


def test_a_running_process_is_alive_and_the_handle_is_closed(no_kill):
    k = FakeKernel32(wait=0x102)                       # WAIT_TIMEOUT: not signalled
    assert parent_watch._alive_windows(4242, k, lambda: 0) is True
    access, inherit, pid = k.opened[0]
    assert pid == 4242 and inherit is False
    assert access == 0x00100000 | 0x1000               # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION only
    assert k.waited == [(1234, 0)]                      # a poll, never a blocking wait
    assert k.closed == [1234]


def test_an_exited_process_is_gone_and_the_handle_is_closed(no_kill):
    k = FakeKernel32(wait=0)                           # WAIT_OBJECT_0: signalled = exited
    assert parent_watch._alive_windows(4242, k, lambda: 0) is False
    assert k.closed == [1234]


def test_no_such_process_is_gone(no_kill):
    k = FakeKernel32(handle=0)
    assert parent_watch._alive_windows(4242, k, lambda: 87) is False   # ERROR_INVALID_PARAMETER
    assert k.waited == [] and k.closed == []


@pytest.mark.parametrize("err", [5, 0, 1, 31])
def test_other_failures_count_as_alive_never_as_a_reason_to_stop(err, no_kill):
    k = FakeKernel32(handle=0)                         # e.g. ERROR_ACCESS_DENIED (5)
    assert parent_watch._alive_windows(4242, k, lambda: err) is True


@pytest.mark.parametrize("pid", [0, -1])
def test_invalid_pids_are_gone(pid, no_kill):
    k = FakeKernel32()
    assert parent_watch._alive_windows(pid, k, lambda: 0) is False
    assert k.opened == []


def test_posix_still_uses_signal_zero(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append((pid, sig)))
    assert parent_watch.alive(77) is True and calls == [(77, 0)]

    def gone(pid, sig):
        raise ProcessLookupError
    monkeypatch.setattr(os, "kill", gone)
    assert parent_watch.alive(77) is False

    def denied(pid, sig):
        raise PermissionError
    monkeypatch.setattr(os, "kill", denied)
    assert parent_watch.alive(77) is True


@windows_only
def test_real_windows_liveness_does_not_terminate_the_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        for _ in range(5):
            assert parent_watch.alive(child.pid) is True
        assert child.poll() is None, "checking liveness terminated the process"
    finally:
        child.kill()
        child.wait()
    assert parent_watch.alive(child.pid) is False
    assert parent_watch.alive(os.getpid()) is True


# ── the "port in use" guard ──────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_second_bind_to_a_port_in_use_is_refused():
    """The guard of _bind_server: on every platform, including real Windows."""
    port = _free_port()
    first = server._bind_server("127.0.0.1", port)
    try:
        with pytest.raises(SystemExit) as e:
            server._bind_server("127.0.0.1", port)
        assert "Cannot bind" in str(e.value)
    finally:
        first.server_close()


def test_the_server_is_still_a_threading_http_server():
    srv = server._bind_server("127.0.0.1", 0)
    try:
        assert isinstance(srv, server.ThreadingHTTPServer) and srv.daemon_threads
    finally:
        srv.server_close()


class _Recorder:
    def __init__(self):
        self.opts: list[tuple] = []

    def setsockopt(self, *a):
        self.opts.append(a)


def _bind_with(monkeypatch, *, platform):
    import http.server
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", -5, raising=False)
    bound = []
    monkeypatch.setattr(http.server.HTTPServer, "server_bind", lambda self: bound.append(self.allow_reuse_address))
    srv = server._LocalHTTPServer.__new__(server._LocalHTTPServer)
    srv.socket = _Recorder()
    srv.server_bind()
    return srv, bound


def test_on_windows_the_port_is_taken_exclusively_and_not_shared(monkeypatch):
    srv, bound = _bind_with(monkeypatch, platform="win32")
    assert srv.socket.opts == [(socket.SOL_SOCKET, -5, 1)]       # SO_EXCLUSIVEADDRUSE
    assert bound == [False]                                       # SO_REUSEADDR is not set


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_elsewhere_the_bind_is_exactly_what_it_was(monkeypatch, platform):
    srv, bound = _bind_with(monkeypatch, platform=platform)
    assert srv.socket.opts == []                                  # nothing extra
    assert bound == [True]                                        # HTTPServer's SO_REUSEADDR


# ── owner-only permissions ───────────────────────────────────────────────────

@posix_only
def test_files_and_dirs_are_restricted_on_posix(tmp_path):
    f = tmp_path / "secret"
    f.write_text("x")
    f.chmod(0o644)
    assert fileperms.restrict_to_owner(f) is True
    assert (f.stat().st_mode & 0o777) == 0o600
    d = tmp_path / "d"
    d.mkdir()
    assert fileperms.restrict_to_owner(d, 0o700) is True
    assert (d.stat().st_mode & 0o777) == 0o700


def test_on_windows_chmod_is_skipped_and_nothing_fails(monkeypatch, tmp_path):
    f = tmp_path / "secret"
    f.write_text("x")
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(self, mode):
        raise AssertionError("chmod does nothing useful on Windows and must not be called")
    monkeypatch.setattr(Path, "chmod", boom)
    assert fileperms.restrict_to_owner(f) is False
    assert fileperms.restrict_to_owner(tmp_path / "missing") is False


def test_a_filesystem_that_refuses_chmod_does_not_break_saving(monkeypatch, tmp_path):
    f = tmp_path / "secret"
    f.write_text("x")

    def refuse(self, mode):
        raise PermissionError("not permitted")
    monkeypatch.setattr(Path, "chmod", refuse)
    assert fileperms.restrict_to_owner(f) is False
    assert fileperms.restrict_to_owner(tmp_path / "missing") is False


def test_the_call_sites_use_the_helper(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(fileperms, "restrict_to_owner", lambda p, mode=0o600: seen.append(Path(p).name) or True)
    from ops_dashboard import brain, secret_vault, sync_bundle
    for mod in (brain, secret_vault, sync_bundle):
        assert mod.restrict_to_owner is not None
    sources = {m: Path(m.__file__).read_text() for m in (brain, secret_vault, sync_bundle)}
    for m, src in sources.items():
        assert "restrict_to_owner(" in src and ".chmod(0o600)" not in src, m.__name__
    assert ".chmod(0o600)" not in (ROOT / "ops_dashboard" / "learning" / "store.py").read_text()


# ── the Rust side: random_hex32 on Windows ───────────────────────────────────

def test_random_hex32_has_a_windows_path_and_macos_linux_are_unchanged():
    src = (ROOT / "desktop" / "src-tauri" / "src" / "lib.rs").read_text()
    body = src[src.index("fn random_hex32()"):]
    body = body[:body.index("\n}\n")]
    assert '#[cfg(unix)]' in body and 'File::open("/dev/urandom")' in body
    win = body[body.index("#[cfg(windows)]"):body.index("#[cfg(not(any(unix, windows)))]")]
    assert "getrandom::fill(&mut buf)" in win and "let mut buf = [0u8; 32];" in win
    assert "None" in body[body.index("#[cfg(not(any(unix, windows)))]"):]


def test_getrandom_is_a_windows_only_direct_dependency_and_locked():
    toml = (ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text()
    m = re.search(r"\[target\.'cfg\(windows\)'\.dependencies\]\n(.*?)\n\n", toml, re.S)
    assert m and m.group(1).strip() == 'getrandom = "0.3"'
    assert "getrandom" not in toml.replace(m.group(0), "")
    lock = (ROOT / "desktop" / "src-tauri" / "Cargo.lock").read_text()
    pkg = lock[lock.index('name = "conductloop-desktop"'):]
    pkg = pkg[:pkg.index("[[package]]")]
    assert '"getrandom 0.3.4"' in pkg
