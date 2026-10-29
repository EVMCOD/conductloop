"""Per-platform capabilities (ops_dashboard/platform_caps.py).

``sys.platform`` is monkeypatched, so all three platforms are checked on any
CI runner. This does NOT run anything on Windows or macOS: it checks the
decisions, not the real OS behaviour (that stays marked untested).
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ops_dashboard import platform_caps as pc
from ops_dashboard.sessions import SessionStore, sandbox

ROOT = Path(__file__).resolve().parent.parent

# (sys.platform, id, browser, code sessions)
PLATFORMS = [
    ("darwin", "macos", True, True),
    ("linux", "linux", False, True),
    ("linux2", "linux", False, True),
    ("win32", "windows", False, False),
    ("cygwin", "windows", False, False),
    ("freebsd14", "other", False, True),
]


@pytest.mark.parametrize("plat,pid,browser,code", PLATFORMS)
def test_platform_flags(monkeypatch, plat, pid, browser, code):
    monkeypatch.setattr("sys.platform", plat)
    assert pc.platform_id() == pid
    assert pc.browser_supported() is browser
    assert pc.code_sessions_supported() is code


@pytest.mark.parametrize("plat,pid,browser,code", PLATFORMS)
def test_capabilities_payload(monkeypatch, plat, pid, browser, code):
    monkeypatch.setattr("sys.platform", plat)
    monkeypatch.delenv("CONDUCTLOOP_SANDBOX", raising=False)
    caps = pc.capabilities(backend="bwrap" if code else None)
    assert caps["platform"] == pid
    assert caps["browser"]["available"] is browser
    assert caps["browser"]["reason"] == (None if browser else "platform_unsupported")
    assert bool(caps["browser"]["message"]) is (not browser)
    ce = caps["code_execution"]
    assert ce["available"] is code
    assert ce["reason"] == (None if code else "platform_unsupported")
    assert bool(ce["message"]) is (not code)


@pytest.mark.parametrize("plat", ["darwin", "linux"])
def test_missing_or_disabled_sandbox_turns_commands_off(monkeypatch, plat):
    monkeypatch.setattr("sys.platform", plat)
    monkeypatch.delenv("CONDUCTLOOP_SANDBOX", raising=False)
    ce = pc.capabilities(backend=None)["code_execution"]
    assert ce["available"] is False and ce["reason"] == "sandbox_missing"
    assert "never runs commands without a sandbox" in ce["message"]
    monkeypatch.setenv("CONDUCTLOOP_SANDBOX", "none")
    assert pc.capabilities(backend=None)["code_execution"]["reason"] == "sandbox_disabled"


def test_messages_name_the_platform_and_why(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    assert "Windows" in pc.browser_message() and "macOS 14" in pc.browser_message()
    assert "Windows" in pc.code_message("platform_unsupported")
    monkeypatch.setattr("sys.platform", "linux")
    assert "bubblewrap" in pc.code_message("sandbox_missing")
    monkeypatch.setattr("sys.platform", "darwin")
    assert pc.browser_message() == ""
    assert "sandbox-exec" in pc.code_message("sandbox_missing")


def test_require_code_sessions_only_refuses_windows(monkeypatch):
    for plat in ("darwin", "linux", "freebsd14"):
        monkeypatch.setattr("sys.platform", plat)
        pc.require_code_sessions()
    monkeypatch.setattr("sys.platform", "win32")
    with pytest.raises(pc.PlatformUnsupported) as e:
        pc.require_code_sessions()
    assert e.value.code == "platform_unsupported" and e.value.capability == "code_execution"


# ── sandbox: never runs without one ────────────────────────────────────────

def test_windows_has_no_sandbox_backend_even_if_bwrap_is_on_path(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delenv("CONDUCTLOOP_SANDBOX", raising=False)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sandbox, "_probe", lambda name, argv: True)
    monkeypatch.setattr(sandbox.os.path, "exists", lambda p: True)
    assert sandbox.detect_backend() is None


@pytest.mark.parametrize("kwargs", [
    {}, {"allow_unsandboxed": True}, {"backend": "bwrap"}, {"backend": "sandbox-exec"},
    {"backend": "bwrap", "allow_unsandboxed": True},
])
def test_windows_refuses_to_run_commands_whatever_the_flags(monkeypatch, tmp_path, kwargs):
    monkeypatch.setattr("sys.platform", "win32")
    popen = []
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **k: popen.append(a))
    with pytest.raises(sandbox.SandboxUnavailable) as e:
        sandbox.run(["python", "-c", "print(1)"], worktree=tmp_path, cwd=tmp_path,
                    path_dirs=[], **kwargs)
    assert "Windows" in str(e.value)
    assert popen == []


def test_unsandboxed_still_refused_on_posix_without_flag(monkeypatch, tmp_path):
    # Unchanged behaviour: no backend + no explicit flag = refused.
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("CONDUCTLOOP_SANDBOX", "none")
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.run(["true"], worktree=tmp_path, cwd=tmp_path, path_dirs=[])


# ── sessions are refused on Windows ───────────────────────────────────────

@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    dbp = tmp_path / "s.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    return tmp_path


def test_supervisor_does_not_start_code_sessions_on_windows(db_env, monkeypatch):
    from ops_dashboard.sessions.supervisor import Supervisor
    repo = db_env / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    store = SessionStore()
    store.mark_repo(str(repo))
    sid = store.create_session(name="a", role="a", provider="mock", model="m",
                               repo_path=str(repo), base_ref="main", budget_cost_cents=100)
    store.set_session_worktree(sid, str(db_env / "wt"), "b")
    before = store.get_session(sid)["status"]
    sup = Supervisor(store, poll_s=0.05)
    monkeypatch.setattr("sys.platform", "win32")
    with pytest.raises(pc.PlatformUnsupported):
        sup.start(sid)
    assert sup.running() == []
    assert store.get_session(sid)["status"] == before


@pytest.fixture()
def server(db_env):
    from ops_dashboard.server import DashboardHandler

    class Quiet(DashboardHandler):
        def log_message(self, *a):
            pass
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _call(url, data=None):
    req = urllib.request.Request(url, method="POST" if data is not None else "GET",
                                 data=None if data is None else json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_platform_endpoint(server):
    status, body = _call(server + "/api/platform")
    assert status == 200 and body["ok"] is True
    assert body["platform"] in {"macos", "linux", "windows", "other"}
    assert set(body) >= {"browser", "code_execution", "keychain"}


def test_platform_endpoint_reports_windows(server, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    status, body = _call(server + "/api/platform")
    assert status == 200 and body["platform"] == "windows"
    assert body["browser"]["available"] is False
    assert body["code_execution"]["available"] is False
    assert body["code_execution"]["reason"] == "platform_unsupported"


def test_creating_a_code_session_on_windows_is_refused_with_a_code(server, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    status, body = _call(server + "/api/sessions", {"role": "x", "provider": "mock", "model": "m"})
    assert status == 409
    assert body["error"] == "platform_unsupported" or body.get("code") == "platform_unsupported"
    assert "sandbox" in body["message"]


# ── static: the Rust gate and the UI text ──────────────────────────────────

def test_rust_browser_is_off_outside_macos_and_macos_check_is_unchanged():
    src = (ROOT / "desktop" / "src-tauri" / "src" / "browser.rs").read_text()
    fn = src[src.index("fn os_supported()"):src.index("#[derive(Serialize)]\npub struct BrowserList")]
    assert "major >= 14" in fn
    assert re.search(r'#\[cfg\(not\(target_os = "macos"\)\)\]\s*\{\s*false\s*\}', fn)
    assert 'Err("macos_14_required".into())' in src
    assert 'Err("platform_unsupported".into())' in src
    assert "fn in_app_browser_is_off_outside_macos" in src


def test_browser_page_explains_platform_unsupported():
    html = (ROOT / "ops_dashboard" / "web" / "browser.html").read_text()
    assert '"platform_unsupported"' in html and "/api/platform" in html
    assert '"macos_14_required": "The in-app browser needs macOS 14 (Sonoma) or later."' in html
