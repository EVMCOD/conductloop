"""ConductLoop is free: no license gate, no license-worker calls from the app.

Offline. Boots the real handler on an ephemeral port (like test_server_smoke)
and fails if anything tries to open a network connection to a remote host.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture()
def lic(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_DISABLE_KEYCHAIN", "1")
    monkeypatch.setenv("OPS_SECRETS_DIR", str(tmp_path / "secrets"))
    from ops_dashboard import licensing
    monkeypatch.setattr(licensing, "CONFIG_PATH", tmp_path / "config.json")
    return licensing


@pytest.fixture()
def no_network(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("licensing must not use the network")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)


def test_status_is_free_without_network(lic, no_network, monkeypatch):
    monkeypatch.setenv("OPS_LICENSE_ENFORCE", "1")  # ignored
    st = lic.status()
    assert st["licensed"] is True and st["enforced"] is False
    assert st["plan"] == "free" and st["edition"] == "free"
    assert "legacy_key_on_file" not in st
    assert lic.enforce() is False


def test_legacy_key_is_flagged_not_sent(lic, no_network, tmp_path):
    # A beta user with an (expired) trial key on file: still free, no errors.
    (tmp_path / "config.json").write_text(json.dumps(
        {"license_key": "CNDL-OLD-TRIAL", "license_email": "old@example.invalid",
         "license_expires_at": "2026-07-20T00:00:00Z"}))
    st = lic.status()
    assert st["licensed"] is True and st["plan"] == "free"
    assert st["legacy_key_on_file"] is True
    assert "key" not in st  # the key itself is never echoed


def test_worker_client_is_gone(lic):
    for name in ("start_trial", "activate", "checkout", "_post", "WORKER_URL", "GRACE_SECONDS"):
        assert not hasattr(lic, name), name


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("ops") / "lic.db"
    os.environ["CONDUCTLOOP_DB_PATH"] = str(db_path)
    from ops_dashboard import db
    from ops_dashboard.server import DashboardHandler

    db.DB_PATH = db_path  # type: ignore[attr-defined]
    db.init_db()

    class _Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


def _req(url: str, method: str = "GET", body: dict | None = None):
    opener = urllib.request.build_opener(_NoRedirect)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=8) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_root_never_redirects_to_license(base_url, monkeypatch):
    monkeypatch.setenv("OPS_LICENSE_ENFORCE", "1")
    status, headers, _ = _req(base_url + "/")
    assert status == 200
    assert "/license" not in headers.get("Location", "")


def test_license_page_redirects_home(base_url):
    status, headers, _ = _req(base_url + "/license")
    assert status == 302 and headers.get("Location") == "/"


def test_license_status_is_free(base_url):
    status, _, body = _req(base_url + "/api/license/status")
    assert status == 200 and json.loads(body)["plan"] == "free"


@pytest.mark.parametrize("path", ["/api/license/trial", "/api/license/activate", "/api/license/checkout"])
def test_license_worker_proxies_removed(base_url, path):
    status, _, body = _req(base_url + path, "POST", {"email": "x@example.invalid", "key": "K"})
    assert status != 200, (path, body[:200])
