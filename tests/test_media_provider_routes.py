"""The provider API: choosing who does the work, and setting them up.

Two things are being protected here. The first is that a request may name a
capability rather than a vendor, and the second is that the credentials of
one provider never leak into another provider's settings — or back to the
client.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import (
    media,
    routes_media,  # noqa: F401  (registers the routes)
)
from ops_dashboard import media_providers as mp


@pytest.fixture
def server(tmp_path, monkeypatch):
    from ops_dashboard import db
    from ops_dashboard.server import DashboardHandler

    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(media.app_paths, "data_dir", lambda: tmp_path)
    db.init_db()
    media.set_engine(None)

    class _Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        with db.db() as conn:
            conn.execute("DELETE FROM media_providers")


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=20) as r:
        return r.status, json.loads(r.read())


def _post(base: str, path: str, payload: dict):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _delete(base: str, path: str):
    req = urllib.request.Request(base + path, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ── the catalogue ─────────────────────────────────────────────────────────

def test_the_catalogue_lists_every_provider_and_what_it_can_do(server):
    status, data = _get(server, "/api/media/providers")
    assert status == 200
    keys = {p["key"] for p in data["providers"]}
    assert {"matrix", "openai", "ollama", "comfyui", "macos_say"} <= keys
    for provider in data["providers"]:
        assert provider["capabilities"], f"{provider['key']} declares nothing"
        assert "api_key" not in json.dumps(provider["default_config"])


def test_the_catalogue_says_which_capability_each_provider_serves(server):
    _status, data = _get(server, "/api/media/providers")
    capabilities = data["capabilities"]
    assert "image" in capabilities and "matrix" in capabilities["image"]
    assert "text" in capabilities and "ollama" in capabilities["text"]


def test_a_provider_can_be_configured_and_switched_on(server):
    status, data = _post(server, "/api/media/providers/ollama", {
        "config": {"base_url": "http://127.0.0.1:11434", "model": "llama3.2"},
        "enabled": True,
    })
    assert status == 200
    assert data["provider"]["enabled"] is True
    assert data["provider"]["default_config"]["model"] == "llama3.2"


def test_the_api_key_is_accepted_and_never_returned(server):
    status, data = _post(server, "/api/media/providers/stability", {
        "config": {"api_key": "sk-super-secret"}, "enabled": True,
    })
    assert status == 200
    assert "sk-super-secret" not in json.dumps(data)
    # …and it is not in the catalogue either.
    _status, listing = _get(server, "/api/media/providers")
    assert "sk-super-secret" not in json.dumps(listing)


def test_an_empty_key_keeps_the_one_that_is_stored(server):
    _post(server, "/api/media/providers/stability", {
        "config": {"api_key": "sk-keep-me"}, "enabled": True})
    _post(server, "/api/media/providers/stability", {
        "config": {"api_key": "", "model": "sd3"}, "enabled": True})
    stored = mp.stored_configs()["stability"]["config"]
    assert stored["api_key"] == "sk-keep-me"
    assert stored["model"] == "sd3"


def test_a_provider_can_be_switched_off(server):
    _post(server, "/api/media/providers/ollama", {"enabled": True})
    status, data = _delete(server, "/api/media/providers/ollama")
    assert status == 200
    assert data["enabled"] is False
    assert "ollama" not in mp.enabled_keys("text")


def test_an_unknown_provider_is_a_404(server):
    status, _data = _post(server, "/api/media/providers/nope", {"enabled": True})
    assert status == 404


# ── choosing a provider for a render ──────────────────────────────────────

def test_the_image_endpoint_honours_the_chosen_provider(server, monkeypatch):
    """A named provider is the one that does the work."""
    seen: list[str] = []

    class _Fake(mp.Provider):
        spec = mp.ProviderSpec(key="fake", label="Fake", capabilities=(mp.IMAGE,))

        def generate_image(self, prompt, **kw):
            seen.append(self.spec.key)
            path = media.media_dir("demo") / "f.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x89PNG\r\n\x1a\nx")
            return mp.MediaResult(ok=True, kind="image", path=str(path),
                                  provider="fake", meta={"paths": [str(path)]})

    monkeypatch.setattr(mp, "pick", lambda capability, key=None: _Fake())
    status, data = _post(server, "/api/media/image", {"prompt": "un gato",
                                                       "provider": "fake"})
    assert status == 200
    assert seen == ["fake"]
    assert data["meta"]["provider"] == "fake"


def test_the_status_endpoint_announces_the_providers(server):
    status, data = _get(server, "/api/media/status")
    assert status == 200
    assert data["providers"]
    assert data["capabilities"]["image"]
