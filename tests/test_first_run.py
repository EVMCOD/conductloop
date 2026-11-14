"""First run: the vault chosen in the app, the welcome page, the AI key prompt.

The key must never come back out: not in a response, not in a log, not in
config.json, not on disk in plain text.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import app_paths, brain, db, library, onboarding, secret_vault

SENTINEL = "sk-ant-SENTINEL-do-not-leak-0123456789"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A machine that has just had the app installed: empty config, empty database."""
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("OPS_CONFIG_PATH", str(cfg))
    monkeypatch.setattr(brain, "CONFIG_PATH", cfg)
    for var in ("OPS_VAULT_ROOT", "OPS_EXTRA_VAULTS", "OPS_WORKSPACE",
                "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MINIMAX_API_KEY", "GATEWAY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    default = tmp_path / "default-vault"
    monkeypatch.setattr(db, "DEFAULT_VAULT", default)
    dbp = tmp_path / "ops.db"
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    return {"tmp": tmp_path, "cfg": cfg, "default": default}


def _config(home) -> dict:
    return json.loads(home["cfg"].read_text()) if home["cfg"].exists() else {}


# --- the vault: environment, then the app's choice, then the default ---------

def test_vault_root_precedence_env_then_config_then_default(home, monkeypatch):
    assert db.vault_root() == home["default"] and db.vault_root_source() == "default"
    chosen = home["tmp"] / "mine"
    home["cfg"].write_text(json.dumps({"vault_root": str(chosen)}))
    assert db.vault_root() == chosen and db.vault_root_source() == "config"
    forced = home["tmp"] / "forced"
    monkeypatch.setenv("OPS_VAULT_ROOT", str(forced))
    assert db.vault_root() == forced and db.vault_root_source() == "env"


def test_an_empty_variable_or_a_broken_config_falls_through(home, monkeypatch):
    monkeypatch.setenv("OPS_VAULT_ROOT", "   ")
    home["cfg"].write_text("{not json")
    assert db.vault_root() == home["default"]
    home["cfg"].write_text(json.dumps({"vault_root": 42}))
    assert db.vault_root() == home["default"] and db.vault_root_source() == "default"


def test_vault_root_expands_a_tilde(home):
    home["cfg"].write_text(json.dumps({"vault_root": "~/some-notes"}))
    assert "~" not in str(db.vault_root())


def test_saving_the_write_vault_needs_a_folder_unless_asked_to_create_it(home):
    target = home["tmp"] / "new" / "vault"
    with pytest.raises(ValueError, match="not a folder"):
        library.save_settings(write_vault=str(target))
    assert not target.exists() and "vault_root" not in _config(home)
    saved = library.save_settings(write_vault=str(target), create_write_vault=True)
    assert target.is_dir() and _config(home)["vault_root"] == str(target)
    assert saved["write_vault"] == str(target) and saved["write_vault_source"] == "config"
    assert saved["write_vault_exists"] is True


def test_a_relative_write_vault_is_refused_and_empty_goes_back_to_default(home):
    with pytest.raises(ValueError, match="full path"):
        library.save_settings(write_vault="notes", create_write_vault=True)
    chosen = home["tmp"] / "mine"
    chosen.mkdir()
    library.save_settings(write_vault=str(chosen))
    assert db.vault_root() == chosen
    library.save_settings(write_vault="")
    assert db.vault_root() == home["default"]
    assert "vault_root" not in _config(home)


def test_saving_the_vault_keeps_the_other_settings(home):
    other = home["tmp"] / "other"
    other.mkdir()
    home["cfg"].write_text(json.dumps({"default_provider": "anthropic", "index_folders": [str(other)]}))
    library.save_settings(write_vault=str(home["tmp"] / "v"), create_write_vault=True)
    cfg = _config(home)
    assert cfg["default_provider"] == "anthropic" and cfg["index_folders"] == [str(other)]


def test_agent_artifacts_and_summaries_follow_the_vault_chosen_now(home):
    from ops_dashboard import tools
    from ops_dashboard.indexers import ai_summary
    first = home["tmp"] / "first"
    second = home["tmp"] / "second"
    library.save_settings(write_vault=str(first), create_write_vault=True)
    assert tools._agent_vault_root() == first / "_agents"
    assert ai_summary.summary_dir() == first / "_agents" / "daily-summaries"
    library.save_settings(write_vault=str(second), create_write_vault=True)
    assert tools._agent_vault_root() == second / "_agents"


def test_importing_the_summary_module_creates_no_folder(home):
    import importlib

    from ops_dashboard.indexers import ai_summary
    importlib.reload(ai_summary)
    assert not home["default"].exists()


# --- who is on a first run ----------------------------------------------------

def test_a_clean_install_is_fresh(home):
    s = onboarding.state()
    assert s["fresh"] is True and s["documents"] == 0 and s["has_key"] is False
    assert s["vault_source"] == "default" and s["vault_exists"] is False


def test_nobody_who_is_already_set_up_is_sent_to_the_welcome_page(home, monkeypatch):
    # documents
    db.upsert_document(source="obsidian", path="/n/a.md", title="a", content="x",
                       metadata=None, mtime=1, size=1)
    assert onboarding.state()["fresh"] is False
    with db.get_connection() as conn:
        conn.execute("DELETE FROM documents")
        conn.commit()
    assert onboarding.state()["fresh"] is True
    # a key in the environment, a key reference, a plain key
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert onboarding.state()["fresh"] is False and onboarding.state()["has_key"] is True
    monkeypatch.delenv("OPENAI_API_KEY")
    for field in ("anthropic_api_key_ref", "minimax_api_key"):
        home["cfg"].write_text(json.dumps({field: "something"}))
        assert onboarding.state()["fresh"] is False, field
    # any setting at all, a vault from the environment, a folder to index
    home["cfg"].write_text(json.dumps({"default_provider": "ollama"}))
    assert onboarding.state()["fresh"] is False
    home["cfg"].write_text("{}")
    monkeypatch.setenv("OPS_VAULT_ROOT", str(home["tmp"] / "v"))
    assert onboarding.state()["fresh"] is False and onboarding.state()["vault_source"] == "env"
    monkeypatch.delenv("OPS_VAULT_ROOT")
    folder = home["tmp"] / "notes"
    folder.mkdir()
    monkeypatch.setenv("OPS_EXTRA_VAULTS", str(folder))
    assert onboarding.state()["fresh"] is False


@pytest.mark.parametrize("kind", ["dismissed", "completed"])
def test_finishing_or_skipping_is_remembered_and_keeps_other_settings(home, kind):
    home["cfg"].write_text("{}")
    assert onboarding.state()["fresh"] is True
    onboarding.mark(kind)
    s = onboarding.state()
    assert s["fresh"] is False and s[kind] is True
    assert _config(home)["onboarding"][f"{kind}_at"]
    onboarding.mark("completed" if kind == "dismissed" else "dismissed")
    assert set(_config(home)["onboarding"]) == {"dismissed_at", "completed_at"}


def test_mark_rejects_anything_else(home):
    with pytest.raises(ValueError):
        onboarding.mark("whatever")


def test_the_checklist_points_at_the_page_instead_of_an_environment_variable(home):
    items = {i["id"]: i for i in onboarding.checklist()["checks"]}
    assert "OPS_VAULT_ROOT" not in items["vault"]["detail"]
    assert "Setup" in items["vault"]["detail"] or "welcome" in items["vault"]["detail"]


# --- the server ---------------------------------------------------------------

@pytest.fixture()
def server(home):
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


def _call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def test_the_welcome_page_is_served_with_the_strict_policy(server):
    status, body, headers = _call(server, "GET", "/welcome")
    assert status == 200 and b"/welcome.js" in body and b"Welcome to ConductLoop" in body
    csp = headers.get_all("Content-Security-Policy")
    assert len(csp) == 1 and "'unsafe-inline'" not in csp[0] and "script-src 'self'" in csp[0]
    assert _call(server, "GET", "/welcome.css")[0] == 200
    assert _call(server, "GET", "/welcome.js")[0] == 200
    assert _call(server, "GET", "/first-run.js")[0] == 200


def test_the_quick_tour_is_still_at_tour(server):
    status, body, _ = _call(server, "GET", "/tour")
    assert status == 200 and b"Quick Day-1 Wins" in body


def test_state_dismiss_and_complete_over_http(server, home):
    status, body, _ = _call(server, "GET", "/api/onboarding/state")
    s = json.loads(body)
    assert status == 200 and s["ok"] is True and s["fresh"] is True
    status, body, _ = _call(server, "POST", "/api/onboarding/dismiss", {})
    assert status == 200 and json.loads(body)["fresh"] is False
    assert _call(server, "POST", "/api/onboarding/complete", {})[0] == 200
    assert "completed_at" in _config(home)["onboarding"]


def test_choosing_the_vault_over_http(server, home):
    target = home["tmp"] / "chosen"
    status, body, _ = _call(server, "POST", "/api/library",
                            {"write_vault": str(target), "create_write_vault": True})
    out = json.loads(body)
    assert status == 200 and out["write_vault"] == str(target) and target.is_dir()
    status, body, _ = _call(server, "GET", "/api/library")
    assert json.loads(body)["write_vault_source"] == "config"
    assert _config(home)["vault_root"] == str(target)
    # persistent: a new process reads the same file
    assert db.vault_root() == target


def test_the_vault_route_refuses_what_it_cannot_use(server, home):
    assert _call(server, "POST", "/api/library", {"write_vault": 5})[0] == 400
    status, body, _ = _call(server, "POST", "/api/library", {"write_vault": str(home["tmp"] / "nope")})
    assert status == 400 and "not a folder" in json.loads(body)["message"]
    assert "vault_root" not in _config(home)


def test_create_is_only_honoured_for_a_real_true(server, home):
    target = home["tmp"] / "x"
    status, _, _ = _call(server, "POST", "/api/library",
                         {"write_vault": str(target), "create_write_vault": "yes"})
    assert status == 400 and not target.exists()


# --- the key never comes back out ---------------------------------------------

def _tree_text(root) -> list[str]:
    hits = []
    for path in root.rglob("*"):
        if path.is_file():
            try:
                if SENTINEL.encode() in path.read_bytes():
                    hits.append(str(path))
            except OSError:
                pass
    return hits


def test_the_key_is_stored_but_never_echoed_logged_or_written_in_plain(server, home, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    status, body, headers = _call(server, "POST", "/api/credentials/byok.anthropic.default",
                                  {"value": SENTINEL})
    assert status == 200 and json.loads(body)["ok"] is True
    assert SENTINEL.encode() not in body
    assert SENTINEL not in str(dict(headers))
    # the state the welcome page reads afterwards says there is a key, not which
    status, body, _ = _call(server, "GET", "/api/onboarding/state")
    assert json.loads(body)["has_key"] is True and SENTINEL.encode() not in body
    status, body, _ = _call(server, "GET", "/api/credentials")
    assert SENTINEL.encode() not in body
    assert SENTINEL not in caplog.text
    out = capsys.readouterr()
    assert SENTINEL not in out.out + out.err
    assert SENTINEL not in home["cfg"].read_text()
    assert _tree_text(home["tmp"]) == []
    assert _tree_text(app_paths.data_dir()) == []
    # and it is really stored, in the vault
    assert secret_vault.get("byok.anthropic.default") == SENTINEL


def test_a_refused_key_does_not_echo_it_either(server, caplog):
    caplog.set_level(logging.DEBUG)
    status, body, _ = _call(server, "POST", f"/api/credentials/byok.nonsense-{SENTINEL[:8]}.default",
                            {"value": SENTINEL})
    assert status >= 400 and SENTINEL.encode() not in body and SENTINEL not in caplog.text
    status, body, _ = _call(server, "POST", "/api/credentials/byok.anthropic.default", {"value": "   "})
    assert status == 400
