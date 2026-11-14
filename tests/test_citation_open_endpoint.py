"""The citation click, end to end over HTTP.

The click arrives as a POST to /api/brain/citation-open and has to end up as a
row in citation_events attached to the message that owns the citation list. In
the team chat that is not the message the user was reading: the sources belong
to the turn (the plan) while the click arrives on the agent's reply.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture
def server(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ops.db")
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    db.init_db()

    from ops_dashboard.server import DashboardHandler

    class Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _get(base: str, path: str) -> tuple[int, dict]:
    with urllib.request.urlopen(base + path, timeout=15) as r:
        return r.status, json.loads(r.read().decode())


def _team_turn() -> int:
    """A team turn: the user asks, the plan carries the sources, an agent answers."""
    from ops_dashboard import db
    cites = [{"n": 1, "source": "obsidian", "path": "/tmp/ranker/t.md", "title": "T", "score": 0.5}]
    with db.db() as conn:
        conn.execute("INSERT INTO conversations (title, created_at, updated_at) VALUES ('t', 1, 1)")
        conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) "
                     "VALUES (1, 'user', 'mi pregunta', 1)")
        conn.execute("INSERT INTO messages (conversation_id, role, content, citations, created_at) "
                     "VALUES (1, 'plan', 'el plan', ?, 2)", (json.dumps(cites),))
        conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) "
                     "VALUES (1, 'agent:m:1', 'respuesta del agente', 3)")
        return int(conn.execute("SELECT id FROM messages WHERE role='plan'").fetchone()[0])


def test_the_click_lands_on_the_message_that_owns_the_sources(server):
    from ops_dashboard import db
    plan_mid = _team_turn()
    status, out = _post(server, "/api/brain/citation-open",
                        {"answer": "respuesta del agente", "path": "/tmp/ranker/t.md",
                         "source": "obsidian"})
    assert status == 200
    assert out["ok"] is True
    assert out["message_id"] == plan_mid
    with db.db() as conn:
        rows = conn.execute("SELECT path, message_id FROM citation_events").fetchall()
    assert len(rows) == 1
    assert rows[0]["message_id"] == plan_mid
    assert rows[0]["path"] == "/tmp/ranker/t.md"


def test_a_click_that_cannot_be_resolved_is_still_recorded(server):
    """A broken answer text must not throw away the signal."""
    from ops_dashboard import db
    status, out = _post(server, "/api/brain/citation-open",
                        {"answer": "texto que no existe", "path": "/tmp/ranker/x.md"})
    assert status == 200
    assert out["ok"] is True
    assert out["resolved"] is False
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM citation_events").fetchone()[0]
    assert n == 1


def test_a_click_without_a_path_is_rejected(server):
    status, out = _post(server, "/api/brain/citation-open", {"answer": "algo", "path": ""})
    assert status == 400
    assert out["ok"] is False


def test_the_ranker_status_is_readable(server):
    status, out = _get(server, "/api/learning/ranker")
    assert status == 200
    assert out["ok"] is True
    assert out["events"] == 0
    assert out["trained"] is False
    assert out["min_positives"] >= 1


def test_training_without_data_says_so_over_http(server):
    status, out = _post(server, "/api/learning/ranker/train", {})
    assert status == 200
    assert out["ok"] is True
    assert out["report"]["trained"] is False
    assert "not enough data" in out["report"]["reason"]


def test_the_whole_loop_over_http(server):
    """Clicks over HTTP become training data, the model activates, and the
    sources come back in a different order than the search alone would give."""
    from ops_dashboard import db, db_brain
    from ops_dashboard.learning import ranker

    def doc(path: str, title: str) -> None:
        db.upsert_document(source="obsidian", path=path, title=title, content="body",
                           metadata={}, mtime=1_700_000_000, size=4)

    n = 60
    last = None
    for i in range(n):
        q = f"como seGdia el proyecto numero {i} con la parte de metricas"
        good = f"/tmp/ranker/good-{i}.md"
        bad = [f"/tmp/ranker/bad-{i}-{j}.md" for j in range(3)]
        doc(good, f"Como seGdia el proyecto numero {i} con la parte de metricas")
        for b in bad:
            doc(b, f"documento sin relacion numero {i}-{b}")
        cites = [{"n": 1, "source": "obsidian", "path": bad[0], "title": "x", "score": 0.9},
                 {"n": 2, "source": "obsidian", "path": bad[1], "title": "x", "score": 0.7},
                 {"n": 3, "source": "obsidian", "path": bad[2], "title": "x", "score": 0.5},
                 {"n": 4, "source": "obsidian", "path": good, "title": "x", "score": 0.1}]
        answer = f"respuesta {i}"
        db_brain.save_conversation_turn(title="t", user_content=q,
                                        assistant_content=answer, citations=cites)
        # The user opens the source the search had ranked last.
        status, _out = _post(server, "/api/brain/citation-open",
                             {"answer": answer, "path": good, "source": "obsidian"})
        assert status == 200
        last = (q, cites, good)

    status, out = _post(server, "/api/learning/ranker/train", {})
    assert status == 200
    assert out["report"]["trained"] is True
    assert out["report"]["activated"] is True
    assert out["report"]["auc"] > out["report"]["baseline_auc"]
    assert out["trained"] is True          # there is an active model now

    q, cites, good = last
    cands = [{"path": c["path"], "score": c["score"], "title": c["title"]} for c in cites]
    ordered = ranker.rerank(q, cands)
    assert ordered[0]["path"] == good
