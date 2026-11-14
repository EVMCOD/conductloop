"""Mobile pairing over the LAN / Tailscale, from the packaged desktop app.

The companion iPhone app pairs with a QR and then talks to the server over the
LAN (192.168.x.x) or over Tailscale (100.64.0.0/10). The shipped desktop app
could not be reached at all:

* the Tauri shell never told the sidecar to open the network, and
* the frozen entry point (desktop/server_entry.py, the one the release spec
  builds) bound 127.0.0.1 whatever the environment said -- it never looked at
  the opt-in flag the dev server honours.

So a phone could only pair with a server started by hand (OPS_ALLOW_MOBILE=1,
or `ops serve --host 0.0.0.0`).

Opening the port puts the dashboard on the LAN, so this file also pins the one
hole that exposure would otherwise open: account creation. Every other thing
a remote caller can reach already needs a paired-device token or a session.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _load_server_entry():
    """The frozen sidecar's entry point (desktop/conductloop_server.spec)."""
    spec = importlib.util.spec_from_file_location(
        "conductloop_desktop_server_entry", ROOT / "desktop" / "server_entry.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sidecar_binds(monkeypatch, tmp_path, **env) -> str:
    """Run the frozen sidecar's main() and return the host it wanted to bind."""
    from ops_dashboard import db, server

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "sidecar.db")
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    # No scheduler thread and no fixture indexer: only the bind matters here.
    monkeypatch.setenv("CONDUCTLOOP_DEMO", "1")
    monkeypatch.setenv("OPS_HOST", "127.0.0.1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    entry = _load_server_entry()
    bound: dict[str, str] = {}

    def fake_bind(host, port):
        bound["host"] = host
        raise SystemExit(0)

    monkeypatch.setattr(server, "_bind_server", fake_bind)
    with pytest.raises(SystemExit):
        entry.main()
    return bound["host"]


def test_the_shell_asks_the_sidecar_for_mobile_access():
    """The Tauri shell must ask the sidecar to open the network.

    The flag travels in the sidecar's env chain; a mention somewhere else in
    the file would never reach the sidecar, so the assertion looks at that
    chain only.
    """
    lib = (ROOT / "desktop" / "src-tauri" / "src" / "lib.rs").read_text()
    start = lib.index('.sidecar("conductloop-server")')
    end = lib.index(".spawn()", start)
    # "setting": the server follows the person's choice (network_access);
    # until 1.2.22 the shell passed "1" and every install opened the port.
    assert '.env("OPS_ALLOW_MOBILE", "setting")' in lib[start:end]
    assert '.env("OPS_ALLOW_MOBILE", "1")' not in lib[start:end]


def test_the_frozen_sidecar_binds_the_network_when_mobile_access_is_on(monkeypatch, tmp_path):
    """The shipped binary has to open the port, not just the dev server."""
    assert _sidecar_binds(monkeypatch, tmp_path, OPS_ALLOW_MOBILE="1") == "0.0.0.0"


def test_the_frozen_sidecar_stays_on_loopback_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("OPS_ALLOW_MOBILE", raising=False)
    assert _sidecar_binds(monkeypatch, tmp_path) == "127.0.0.1"


def test_an_explicit_host_is_left_alone(monkeypatch):
    """OPS_HOST wins: an operator who asked for one address gets it."""
    from ops_dashboard.server import _resolve_bind

    monkeypatch.setenv("OPS_ALLOW_MOBILE", "1")
    assert _resolve_bind("192.168.1.5", 7777) == "192.168.1.5"


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """The real handler on a real port; `peer` fakes where the request came from."""
    pytest.importorskip("bcrypt")
    from ops_dashboard import db
    from ops_dashboard.indexers import live_urls
    from ops_dashboard.server import DashboardHandler

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "signup.db")
    db.init_db()
    live_urls.ensure_schema()
    servers: list[ThreadingHTTPServer] = []

    def start(peer: str | None = None) -> str:
        class Handler(DashboardHandler):
            def setup(self):
                super().setup()
                if peer:
                    self.client_address = (peer, 51000)

            def log_message(self, *args):
                pass

        port = _free_port()
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        servers.append(httpd)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{port}"

    yield start
    for httpd in servers:
        httpd.shutdown()


def _signup(base: str, email: str, headers: dict | None = None):
    req = urllib.request.Request(
        f"{base}/api/auth/signup",
        method="POST",
        data=json.dumps({"email": email, "password": "correct horse 9"}).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_account_creation_on_the_machine_still_works(server):
    status, body = _signup(server(), "owner@example.com")
    assert status == 200, body
    assert body["user"]["email"] == "owner@example.com"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        # A caller on the LAN can send any header it likes: a forwarded-for
        # that claims to be loopback must not buy it a local identity.
        {"X-Forwarded-For": "127.0.0.1"},
        {"X-Relay": "1"},
    ],
)
def test_account_creation_from_the_network_is_refused(server, headers):
    """The dashboard is on the LAN now: a stranger must not be able to sign up.

    Everything else a remote caller can reach needs a paired-device token or
    a session; account creation was the one door with no lock on it.
    """
    from ops_dashboard import db

    status, body = _signup(server(peer="192.168.50.7"), "stranger@example.com", headers)
    assert status == 403, body
    assert "only allowed on the machine" in (body.get("error") or "")
    with db.get_connection() as conn:
        emails = [r[0] for r in conn.execute("SELECT email FROM users").fetchall()]
    assert "stranger@example.com" not in emails


# ── Network access is the person's choice (1.2.23) ───────────────────────────
# The desktop app passes OPS_ALLOW_MOBILE=setting: the server opens the network
# only if the person turned it on on the Pair a phone page, or (when they never
# chose) if a phone is already paired, so an existing pairing survives the
# update. Everyone else stays on loopback.


def _pair_a_device(tmp_path, monkeypatch):
    from ops_dashboard import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "sidecar.db")
    db.init_db()
    db.create_device_token(name="iPhone")


def test_the_app_stays_on_loopback_until_the_person_turns_the_network_on(monkeypatch, tmp_path):
    assert _sidecar_binds(monkeypatch, tmp_path, OPS_ALLOW_MOBILE="setting") == "127.0.0.1"


def test_the_app_opens_the_network_when_the_person_turned_it_on(monkeypatch, tmp_path):
    from ops_dashboard import network_access

    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    network_access.set_wanted(True)
    assert _sidecar_binds(monkeypatch, tmp_path, OPS_ALLOW_MOBILE="setting") == "0.0.0.0"


def test_an_install_with_a_paired_phone_keeps_it_working(monkeypatch, tmp_path):
    """No stored choice + a phone already paired = the network stays open."""
    _pair_a_device(tmp_path, monkeypatch)
    assert _sidecar_binds(monkeypatch, tmp_path, OPS_ALLOW_MOBILE="setting") == "0.0.0.0"


def test_turning_it_off_wins_over_a_paired_phone(monkeypatch, tmp_path):
    from ops_dashboard import network_access

    _pair_a_device(tmp_path, monkeypatch)
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    network_access.set_wanted(False)
    assert _sidecar_binds(monkeypatch, tmp_path, OPS_ALLOW_MOBILE="setting") == "127.0.0.1"


def test_a_revoked_phone_does_not_keep_the_network_open(monkeypatch, tmp_path):
    from ops_dashboard import db

    _pair_a_device(tmp_path, monkeypatch)
    for d in db.list_device_tokens():
        db.revoke_device_token(d["id"])
    assert _sidecar_binds(monkeypatch, tmp_path, OPS_ALLOW_MOBILE="setting") == "127.0.0.1"


def _call(base: str, method: str, body: dict | None = None, token: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{base}/api/pair/network", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_the_pair_page_switch_stores_the_choice(server, monkeypatch, tmp_path):
    from ops_dashboard import network_access

    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPS_ALLOW_MOBILE", "setting")
    base = server()
    status, body = _call(base, "GET")
    assert status == 200 and body == {"lan_now": False, "lan_next": False, "managed_by_env": False}
    status, body = _call(base, "POST", {"lan": True})
    assert status == 200 and body["lan_next"] is True and body["lan_now"] is False
    assert network_access.stored() is True
    status, body = _call(base, "POST", {"lan": "yes"})
    assert status == 400


@pytest.mark.parametrize("method,body", [("GET", None), ("POST", {"lan": True})])
def test_the_network_switch_is_local_only(server, monkeypatch, tmp_path, method, body):
    """A device on the network must not be able to read or flip the switch,
    not even a paired phone with a valid token."""
    from ops_dashboard import db, network_access

    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPS_ALLOW_MOBILE", "setting")
    base = server(peer="192.168.50.7")
    status, _ = _call(base, method, body)
    assert status in (401, 403)
    status, _ = _call(base, method, body, token=db.create_device_token(name="iPhone"))
    assert status == 403
    assert network_access.stored() is None


def test_the_switch_says_when_the_environment_decides(server, monkeypatch, tmp_path):
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPS_ALLOW_MOBILE", "1")
    base = server()
    status, body = _call(base, "GET")
    assert status == 200 and body["managed_by_env"] is True and body["lan_next"] is True
    status, _ = _call(base, "POST", {"lan": False})
    assert status == 409
