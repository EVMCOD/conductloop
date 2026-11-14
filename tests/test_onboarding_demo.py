"""The sample workspace: what a first-time user can load before they have a
vault or an API key, and how it is removed again.

Two rules this file exists to keep:
  * the sample data never mixes with the user's own notes, and
  * removing it removes every row it wrote (and only those rows).
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import brain, db, onboarding
from ops_dashboard.indexers import demo as demo_mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A machine that has just had the app installed: empty config, empty database."""
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("OPS_CONFIG_PATH", str(cfg))
    monkeypatch.setattr(brain, "CONFIG_PATH", cfg)
    for var in ("OPS_VAULT_ROOT", "OPS_EXTRA_VAULTS",
                "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MINIMAX_API_KEY", "GATEWAY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # An empty workspace, not the one of the machine that runs the test: the
    # memory indexer would otherwise index the developer's own workspace and
    # the sample would look like it had been replaced by real notes.
    empty = tmp_path / "no-workspace"
    empty.mkdir()
    monkeypatch.setenv("OPS_WORKSPACE", str(empty))
    default = tmp_path / "default-vault"
    monkeypatch.setattr(db, "DEFAULT_VAULT", default)
    dbp = tmp_path / "ops.db"
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    return {"tmp": tmp_path, "cfg": cfg, "default": default}


def _documents() -> list[tuple[str, str]]:
    with db.get_connection() as conn:
        return [tuple(r) for r in conn.execute("SELECT source, path FROM documents").fetchall()]


def _titles() -> list[str]:
    with db.get_connection() as conn:
        return [r[0] for r in conn.execute("SELECT title FROM documents").fetchall()]


def test_a_fresh_install_reports_no_sample_data(home):
    state = onboarding.demo_state()
    assert state["demo"] is False
    assert state["demo_documents"] == 0
    assert state["real_documents"] == 0
    assert state["documents"] == 0


def test_loading_the_sample_workspace_fills_it_with_fictional_notes(home):
    result = onboarding.seed_demo()
    assert result["ok"] is True
    assert result["demo"] is True
    assert result["demo_documents"] > 0
    assert result["real_documents"] == 0
    # every row is tagged, so it can be told apart from the user's notes
    sources = {row[0] for row in _documents()}
    assert sources == {demo_mod.DEMO_TAG}


def test_loading_the_sample_workspace_twice_does_not_duplicate_it(home):
    onboarding.seed_demo()
    first = onboarding.demo_state()["demo_documents"]
    onboarding.seed_demo()
    second = onboarding.demo_state()["demo_documents"]
    assert first == second
    assert len(_documents()) == first


def test_the_sample_data_is_removed_again(home):
    onboarding.seed_demo()
    assert onboarding.demo_state()["demo"] is True
    result = onboarding.clear_demo()
    assert result["ok"] is True
    assert result["demo"] is False
    assert result["documents"] == 0
    assert _documents() == []


def test_removing_the_sample_data_never_touches_the_users_own_notes(home):
    """A real note and a sample note live together; clearing one keeps the other."""
    onboarding.seed_demo()
    db.upsert_document(source="obsidian", path="/vault/mine.md", title="Mine",
                       content="# Mine\n", metadata={}, mtime=0, size=7)
    assert onboarding.demo_state()["real_documents"] == 1
    onboarding.clear_demo()
    assert onboarding.demo_state()["documents"] == 1
    assert _documents() == [("obsidian", "/vault/mine.md")]


def test_the_sample_data_is_refused_once_there_are_real_notes(home):
    db.upsert_document(source="obsidian", path="/vault/mine.md", title="Mine",
                       content="# Mine\n", metadata={}, mtime=0, size=7)
    result = onboarding.seed_demo()
    assert result["ok"] is False
    assert result["error"] == "already_has_notes"
    assert onboarding.demo_state()["demo"] is False


# --- the promise the welcome page makes --------------------------------------


def test_the_sample_disappears_when_your_own_notes_are_indexed(home, tmp_path):
    """The welcome page promises the sample is removed "the moment you index
    your own notes". That has to hold however the indexing started: the
    setup page, the scheduler, or the welcome page itself. So the transition
    lives in the shared indexing path, not in the page that seeds the sample.
    """
    from ops_dashboard import library

    mine = tmp_path / "my-vault"
    (mine / "projects").mkdir(parents=True)
    (mine / "projects" / "alpha.md").write_text("# Alpha\nthe user's own note")
    brain.CONFIG_PATH.write_text(json.dumps({"index_folders": [str(mine)]}))

    onboarding.seed_demo()
    assert onboarding.demo_state()["demo"] is True

    library.refresh(embed=False)

    state = onboarding.demo_state()
    assert state["demo"] is False
    assert state["demo_documents"] == 0
    assert state["real_documents"] >= 1
    assert "Alpha" in _titles()


def test_the_sample_stays_while_there_are_no_real_notes(home):
    """The sample is all the person has to look at: it stays until their own
    notes arrive."""
    from ops_dashboard import library

    onboarding.seed_demo()
    library.refresh(embed=False)
    state = onboarding.demo_state()
    assert state["demo"] is True
    assert state["demo_documents"] > 0


def test_indexing_never_takes_the_persons_own_notes_with_it(home, tmp_path):
    """Clearing the sample clears the sample and nothing else."""
    from ops_dashboard import library

    mine = tmp_path / "my-vault"
    (mine / "projects").mkdir(parents=True)
    (mine / "projects" / "alpha.md").write_text("# Alpha\nthe user's own note")
    brain.CONFIG_PATH.write_text(json.dumps({"index_folders": [str(mine)]}))

    onboarding.seed_demo()
    library.refresh(embed=False)

    titles = _titles()
    assert "Alpha" in titles
    assert "The PARA Method" not in titles


def test_the_welcome_page_state_tells_the_ui_what_it_needs(home):
    state = onboarding.state()
    assert state["fresh"] is True
    assert state["demo"] is False
    assert state["real_documents"] == 0

    onboarding.seed_demo()
    state = onboarding.state()
    # sample notes exist, so this is no longer a first run: the welcome page
    # must not grab the user again on every launch
    assert state["fresh"] is False
    assert state["demo"] is True
    assert state["demo_documents"] > 0
    assert state["real_documents"] == 0


def test_the_state_never_leaks_a_key_or_a_secret(home):
    onboarding.seed_demo()
    blob = json.dumps(onboarding.state())
    assert "sk-" not in blob
    assert "api_key" not in blob.lower() or "has_key" in blob


def test_the_sample_workspace_only_writes_tables_it_can_take_away_again(home):
    """Every table the runtime sample writes to must carry a ``source``
    column, because that is the only thing that tells a sample row from a
    real one — and the only way to remove the sample again later.
    """
    with db.get_connection() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        demo_tables = {t for t in demo_mod.DEMO_TAGGED_TABLES if t in tables}
        assert demo_tables, "no table to write the sample data to"
        for table in demo_tables:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            assert "source" in cols, table


def test_the_runtime_sample_does_not_write_anything_it_cannot_remove(home):
    """The full fixture (used for screenshots with its own throwaway database)
    writes to tables the app also uses for real data. The runtime sample must
    not: it lands in the user's own workspace."""
    before = set(_documents())
    onboarding.seed_demo()
    sources = {row[0] for row in _documents()}
    assert sources <= {"obsidian", demo_mod.DEMO_TAG}
    assert before <= sources


# --- over http --------------------------------------------------------------


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
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_the_sample_workspace_can_be_loaded_over_http(server):
    status, body = _call(server, "POST", "/api/onboarding/demo", {})
    assert status == 200
    assert body["ok"] is True
    assert body["demo"] is True
    assert body["demo_documents"] > 0


def test_loading_the_sample_workspace_can_be_taken_away_over_http(server):
    _call(server, "POST", "/api/onboarding/demo", {})
    status, body = _call(server, "POST", "/api/onboarding/demo/clear", {})
    assert status == 200
    assert body["ok"] is True
    assert body["demo"] is False
    assert body["documents"] == 0


def test_the_sample_workspace_is_written_in_the_readers_language(server):
    status, body = _call(server, "POST", "/api/onboarding/demo", {"lang": "es"})
    assert status == 200
    assert body["seeded"]["lang"] == "es"
    assert body["demo_lang"] == "es"
    titles = _titles()
    assert "El método PARA" in titles
    assert "The PARA Method" not in titles


def test_the_sample_workspace_falls_back_to_english(server):
    status, body = _call(server, "POST", "/api/onboarding/demo", {})
    assert status == 200
    assert body["seeded"]["lang"] == "en"
    assert body["demo_lang"] == "en"
    titles = _titles()
    assert "The PARA Method" in titles
    assert "El método PARA" not in titles


def test_an_unknown_language_is_ignored(server):
    status, body = _call(server, "POST", "/api/onboarding/demo", {"lang": "fr"})
    assert status == 200
    assert body["seeded"]["lang"] == "en"


def test_the_state_says_which_language_the_sample_is_in(server):
    _call(server, "POST", "/api/onboarding/demo", {"lang": "es"})
    status, body = _call(server, "GET", "/api/onboarding/state")
    assert status == 200
    assert body["demo_lang"] == "es"


def test_the_state_of_the_sample_workspace_can_be_read_over_http(server):
    status, body = _call(server, "GET", "/api/onboarding/demo")
    assert status == 200
    assert body["demo"] is False
    assert body["real_documents"] == 0


def test_loading_the_sample_workspace_over_http_is_refused_with_real_notes(server, home):
    db.upsert_document(source="obsidian", path="/vault/mine.md", title="Mine",
                       content="# Mine\n", metadata={}, mtime=0, size=7)
    status, body = _call(server, "POST", "/api/onboarding/demo", {})
    assert status == 409
    assert body["error"] == "already_has_notes"


def test_the_demo_fixtures_can_be_loaded_twice_without_duplicates(home):
    """`ops index` and `ops serve` both load the demo fixtures in demo mode:
    a second load must leave the same rows, not a second copy of the feed
    and the code TODOs (code_todos has no unique key, so INSERT OR REPLACE
    never replaced anything)."""
    from ops_dashboard.indexers import live_urls
    live_urls.ensure_schema()

    def counts():
        with db.db() as conn:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("activity_feed", "code_todos")}

    demo_mod.run()
    first = counts()
    assert first["activity_feed"] > 0 and first["code_todos"] > 0
    demo_mod.run()
    assert counts() == first


def test_the_graph_shows_the_sample_notes(home):
    """Someone exploring the sample opened Graph and read "no graph data yet":
    the graph only read Obsidian notes."""
    from ops_dashboard import db_graph
    demo_mod.run_sample("en")
    graph = db_graph.build_relations()
    assert len(graph["nodes"]) == len(demo_mod.DEMO_NOTES)
