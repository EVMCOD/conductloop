"""Library: read-only folders in config.json, an hourly incremental re-index."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import brain, library


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    import ops_dashboard.db as _db
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "lib.db")
    _db.init_db()
    write_vault = tmp_path / "app-vault"
    (write_vault / "daily").mkdir(parents=True)
    (write_vault / "daily" / "2026-10-23.md").write_text("# briefing\nwritten by the app")
    mine = tmp_path / "my-vault"
    (mine / "projects").mkdir(parents=True)
    (mine / "projects" / "alpha.md").write_text("# Alpha\nthe user's own note")
    workspace = tmp_path / "agent-ws"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("# Memory\nprefers short answers")
    monkeypatch.setenv("OPS_VAULT_ROOT", str(write_vault))
    for var in ("OPS_EXTRA_VAULTS", "OPS_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    brain.CONFIG_PATH.write_text("{}")
    yield {"db": _db, "write": write_vault, "mine": mine, "ws": workspace, "tmp": tmp_path}
    brain.CONFIG_PATH.write_text("{}")


def test_folders_come_from_config_without_missing_or_duplicate_ones(lib):
    brain.CONFIG_PATH.write_text(json.dumps({"index_folders": [
        str(lib["mine"]), str(lib["write"]), str(lib["tmp"] / "gone"), str(lib["mine"])]}))
    assert library.index_folders() == [lib["mine"]]


def test_env_still_wins(lib, monkeypatch):
    other = lib["tmp"] / "other"
    other.mkdir()
    brain.CONFIG_PATH.write_text(json.dumps({"index_folders": [str(lib["mine"])], "memory_workspace": str(lib["ws"])}))
    monkeypatch.setenv("OPS_EXTRA_VAULTS", str(other))
    monkeypatch.setenv("OPS_WORKSPACE", str(other))
    assert library.index_folders() == [other] and library.memory_workspace() == other


def test_save_settings_validates_and_clears(lib):
    with pytest.raises(ValueError):
        library.save_settings(index_folders=[str(lib["tmp"] / "nope")])
    saved = library.save_settings(index_folders=[str(lib["mine"])], memory_workspace=str(lib["ws"]))
    assert saved["active_folders"] == [str(lib["mine"])] and saved["active_workspace"] == str(lib["ws"])
    assert library.save_settings(memory_workspace="")["memory_workspace"] == ""


def test_refresh_reads_the_extra_folders_and_workspace(lib):
    library.save_settings(index_folders=[str(lib["mine"])], memory_workspace=str(lib["ws"]))
    out = library.refresh(embed=False)
    assert out["obsidian"]["indexed"] == 2 and out["memory"]["indexed"] == 1
    with lib["db"].db() as conn:
        paths = {r[0] for r in conn.execute("SELECT path FROM documents")}
    assert str(lib["mine"] / "projects" / "alpha.md") in paths and str(lib["ws"] / "MEMORY.md") in paths
    again = library.refresh(embed=False)                       # incremental: nothing changed
    assert again["obsidian"]["indexed"] == 0 and again["obsidian"]["skipped"] == 2
    status = library.status()
    assert status["documents"]["obsidian"] == 2 and status["last_indexed"]["obsidian"]


def test_scheduler_runs_it_every_hour():
    from ops_dashboard import scheduler
    jobs = {name: spec for name, spec, _fn in scheduler._system_jobs()}
    assert jobs["library_index"] == "17 * * * *"


def test_routes(lib):
    from ops_dashboard.server import DashboardHandler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(base + path, method="POST", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=10)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        assert post("/api/library", {"index_folders": [str(lib["tmp"] / "nope")]})[0] == 400
        code, saved = post("/api/library", {"index_folders": [str(lib["mine"])]})
        assert code == 200 and saved["active_folders"] == [str(lib["mine"])]
        assert post("/api/library/index", {})[0] == 202
        for _ in range(50):
            status = json.loads(urllib.request.urlopen(base + "/api/library", timeout=10).read())
            if not status["indexing"]:
                break
            time.sleep(0.1)
        assert status["documents"].get("obsidian") == 2
    finally:
        srv.shutdown()
