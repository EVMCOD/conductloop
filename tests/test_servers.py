"""The servers panel: what the product can talk to, and to whom.

The claim of the product is that it asks for capabilities and not for
vendors. This panel is that claim made visible, so the tests are about
what it says and about what it *does not* do: it never waits for a
network to draw itself, it never spends the user's credit to decorate a
status line, and it never hands a secret back.
"""
from __future__ import annotations

import json

import pytest

from ops_dashboard import media_providers as mp


@pytest.fixture()
def clean_db():
    """The tests share one database: leave no provider behind."""
    from ops_dashboard import db, embeddings

    db.init_db()
    embeddings.ensure_schema()
    yield
    with db.db() as conn:
        conn.execute("DELETE FROM media_providers")


@pytest.fixture()
def brain_config(monkeypatch):
    """A brain with one provider set up, and no network anywhere."""
    from ops_dashboard import brain

    cfg = {"default_provider": "minimax", "minimax_base_url": "https://api.minimax.io/anthropic",
           "minimax_api_key": "sk-test-not-a-real-key"}
    monkeypatch.setattr(brain, "_load_config", lambda: cfg)
    monkeypatch.setattr(brain, "_provider_cooldown_remaining", lambda name: 0)
    return cfg


def test_every_capability_is_listed(clean_db, brain_config):
    """Chat, image, video, speech and search: the five things it can do."""
    from ops_dashboard import servers

    keys = [row["key"] for row in servers.status()["capabilities"]]
    assert keys == ["chat", "image", "video", "speech", "embeddings"]


def test_the_panel_says_which_server_will_answer(clean_db, brain_config):
    """One active server per capability, and the others as alternatives."""
    from ops_dashboard import servers

    rows = {row["key"]: row for row in servers.status()["capabilities"]}
    chat = rows["chat"]
    assert chat["active"]["name"] == "minimax"
    assert chat["state"] == "ready"
    image = rows["image"]
    assert image["active"]
    assert image["active"]["name"] in mp.enabled_keys(mp.IMAGE)
    # The others are still there: the user picks, the product does not hide.
    assert len(image["servers"]) > 1


def test_a_local_server_is_preferred_over_a_remote_one(clean_db, brain_config):
    """The rule of the product: this Mac first, the cloud after."""

    mp.save_config("ollama", {}, enabled=True)
    mp.save_config("openai", {"api_key": "sk-test"}, enabled=True)
    # ollama is local and the openai one is remote, with the same priority:
    # local wins.
    assert mp.enabled_keys(mp.TEXT)[0] == "ollama"
    # An explicit priority still beats everything.
    mp.save_config("openai", {"api_key": "sk-test"}, enabled=True, priority=1)
    assert mp.enabled_keys(mp.TEXT)[0] == "openai"


def test_the_status_never_waits_for_a_network(clean_db, brain_config, monkeypatch):
    """The page opens as fast as the app does: no probing while drawing."""
    from ops_dashboard import servers

    def explode(self):
        raise AssertionError("the status must not ask a server anything")

    monkeypatch.setattr(mp.Provider, "health", explode)
    monkeypatch.setattr(mp.Provider, "available", explode, raising=False)
    rows = servers.status()["capabilities"]
    assert rows


def test_the_probe_asks_the_servers(clean_db, brain_config, monkeypatch):
    """When the user asks, the servers answer — and a broken one says so."""
    from ops_dashboard import servers

    def health(self):
        return False, "the local embedding engine is not installed"

    monkeypatch.setattr(mp.Provider, "health", health)
    answers = servers.probe()
    assert answers
    for rows in answers.values():
        for answer in rows.values():
            assert set(answer) == {"ok", "message"}
            assert isinstance(answer["ok"], bool)


def test_a_remote_server_is_not_asked_to_spend_money(clean_db, brain_config, monkeypatch):
    """The only honest test of a remote key is to use it. The panel does not."""
    from ops_dashboard import servers

    def boom(*args, **kwargs):
        raise AssertionError("a remote server must not be called")

    monkeypatch.setattr(mp, "http_json", boom)
    answer = servers._probe_chat({"name": "minimax", "local": False, "key": "keychain",
                                   "where": "https://api.minimax.io/anthropic"})
    assert answer["ok"] is True
    assert "keychain" in answer["message"]


def test_a_local_server_without_a_key_is_not_a_fallback(clean_db, brain_config):
    """Ollama needs no key, so it is always 'set up' — but it is not there."""
    from ops_dashboard import brain, servers

    cfg = brain._load_config()
    cfg["default_provider"] = "anthropic"
    rows = {row["key"]: row for row in servers.status()["capabilities"]}
    chat = rows["chat"]
    assert chat["state"] == "unconfigured"
    # MiniMax has a key, so it is the one that would answer. Ollama needs no
    # key, but it is not running: it is not a fallback.
    assert chat["fallback"]["name"] == "minimax"


def test_no_secret_ever_leaves(clean_db, brain_config):
    """The panel is a status page: it never hands a key back."""
    from ops_dashboard import servers

    mp.save_config("stability", {"api_key": "sk-secret-value"}, enabled=True)
    blob = json.dumps(servers.status())
    assert "sk-secret-value" not in blob


def test_the_embeddings_say_how_much_is_indexed(clean_db, brain_config):
    """The search that answers without a network: how much of the vault is in."""
    from ops_dashboard import db, servers

    db.upsert_document(source="obsidian", path="/v/one.md", title="One", content="x",
                       metadata={}, mtime=1780000000, size=1)
    rows = {row["key"]: row for row in servers.status()["capabilities"]}
    emb = rows["embeddings"]
    assert emb["indexed"] == 0
    assert emb["total"] >= 1
    assert emb["state"] in ("ready", "unconfigured")


# ── the API ────────────────────────────────────────────────────────────────

@pytest.fixture()
def server(tmp_path, monkeypatch):
    """A real server on a real port: the routes are the contract."""
    import socket
    import threading
    from http.server import ThreadingHTTPServer

    from ops_dashboard import db
    from ops_dashboard.server import DashboardHandler

    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    db.init_db()

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


def test_the_api_answers_with_the_capabilities(server, brain_config):
    """GET /api/servers: the whole picture, without a network."""
    import json
    import urllib.request

    with urllib.request.urlopen(server + "/api/servers", timeout=20) as r:
        data = json.loads(r.read())
    assert data["ok"] is True
    keys = [row["key"] for row in data["capabilities"]]
    assert keys == ["chat", "image", "video", "speech", "embeddings"]


def test_the_api_asks_the_servers_when_told(server, brain_config):
    """POST /api/servers/check: the answers of the servers themselves."""
    import json
    import urllib.request

    req = urllib.request.Request(server + "/api/servers/check", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    assert data["ok"] is True
    assert isinstance(data["answers"], dict)
