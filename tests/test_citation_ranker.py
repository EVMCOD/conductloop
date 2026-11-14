"""The citation re-ranker and the feedback loop that feeds it.

Three things are pinned here:

* A vote in the team chat resolves. The team chat stores the plan as 'plan'
  and each agent reply as 'agent:<id>'; the resolver used to only accept
  'assistant', so every vote there was a 404.
* A citation click is recorded, and it is the label the ranker learns from.
* The ranker never touches the order unless it beats the retrieval score on
  held-out data. A model that does not beat the baseline stays inactive.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ranker.db")
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    db.init_db()
    return tmp_path


def _doc(path: str, title: str, content: str = "body", mtime: int = 1_700_000_000) -> str:
    from ops_dashboard import db
    db.upsert_document(source="obsidian", path=path, title=title, content=content,
                        metadata={}, mtime=mtime, size=len(content))
    return path


def _turn(question: str, answer: str, citations: list[dict] | None = None) -> tuple[int, int]:
    from ops_dashboard import db, db_brain
    conv = db_brain.save_conversation_turn(title="t", user_content=question,
                                           assistant_content=answer, citations=citations)
    with db.db() as conn:
        mid = conn.execute(
            "SELECT id FROM messages WHERE conversation_id=? AND role='assistant'", (conv,)).fetchone()[0]
    return conv, int(mid)


# ---------- the vote reaches the team chat ----------

def test_vote_resolves_a_team_chat_plan(env):
    from ops_dashboard import db, learning
    with db.db() as conn:
        conn.execute("INSERT INTO conversations (title, created_at, updated_at) VALUES ('t', 1, 1)")
        conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) "
                     "VALUES (1, 'plan', 'el plan es asi', 1)")
    mid = learning.resolve_brain_message(answer="el plan es asi")
    assert mid > 0


def test_vote_resolves_an_agent_reply(env):
    from ops_dashboard import db, learning
    with db.db() as conn:
        conn.execute("INSERT INTO conversations (title, created_at, updated_at) VALUES ('t', 1, 1)")
        conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) "
                     "VALUES (1, 'agent:m:589', 'respuesta del agente', 1)")
    mid = learning.resolve_brain_message(answer="respuesta del agente")
    assert mid > 0


def test_a_user_message_is_not_votable(env):
    from ops_dashboard import db, learning
    with db.db() as conn:
        conn.execute("INSERT INTO conversations (title, created_at, updated_at) VALUES ('t', 1, 1)")
        conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) "
                     "VALUES (1, 'user', 'mi pregunta', 1)")
    with pytest.raises(learning.LearningError):
        learning.resolve_brain_message(answer="mi pregunta")


def test_team_message_is_a_supported_target(env):
    from ops_dashboard import learning
    assert "team_message" in learning.store.SUPPORTED_TARGETS
    conv, mid = _turn("q", "a")
    row = learning.record_feedback(target_kind="team_message", target_id=mid, rating=1)
    assert row["target_kind"] == "team_message"


# ---------- the click is recorded ----------

def test_recording_a_citation_event(env):
    from ops_dashboard.learning import ranker
    p = _doc("/tmp/ranker/doc-a.md", "A")
    row = ranker.record_event(path=p, source="obsidian", message_id=7, conversation_id=3)
    assert row["id"] > 0
    assert ranker.count_events() == 1
    # The path is stored as the indexer wrote it: that is the key that joins
    # with documents.path and with the citations.
    assert ranker.events_since(0)[0]["path"] == p


def test_the_event_table_exists_after_migration(env):
    from ops_dashboard import db
    with db.db() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "citation_events" in names
    assert "ranker_model" in names


# ---------- the ranker ----------

def _corpus(n: int = 70) -> list[tuple[int, str, list[dict], str]]:
    """n answers, each with four retrieved documents.

    The document the user opened is the one whose title repeats the words of
    the question, but the retrieval score puts it LAST. So the baseline (the
    score alone) is no help — and a model that reads the title can learn it.
    """
    out = []
    for i in range(n):
        q = f"como seGdia el proyecto numero {i} con la parte de metricas"
        good = f"/tmp/ranker/good-{i}.md"
        bad = [f"/tmp/ranker/bad-{i}-{j}.md" for j in range(3)]
        _doc(good, f"Como seGdia el proyecto numero {i} con la parte de metricas")
        for b in bad:
            _doc(b, f"documento sin relacion numero {i}-{b}")
        cites = ([{"n": 1, "source": "obsidian", "path": bad[0], "title": "x", "score": 0.9},
                  {"n": 2, "source": "obsidian", "path": bad[1], "title": "x", "score": 0.7},
                  {"n": 3, "source": "obsidian", "path": bad[2], "title": "x", "score": 0.5},
                  {"n": 4, "source": "obsidian", "path": good, "title": "x", "score": 0.1}])
        out.append((i, q, cites, good))
    return out


def test_training_refuses_without_data(env):
    from ops_dashboard.learning import ranker
    report = ranker.train()
    assert report["trained"] is False
    assert report["activated"] is False
    assert "not enough data" in report["reason"]
    assert ranker.active_model() is None


def test_training_activates_a_model_that_beats_the_baseline(env):
    from ops_dashboard.learning import ranker
    for i, q, cites, good in _corpus():
        _turn(q, f"respuesta {i}", citations=cites)
        with __import__("ops_dashboard.db", fromlist=["db"]).db() as conn:
            mid = conn.execute(
                "SELECT id FROM messages WHERE role='assistant' ORDER BY id DESC LIMIT 1").fetchone()[0]
        ranker.record_event(path=good, source="obsidian", message_id=mid)
    report = ranker.train()
    assert report["trained"] is True
    assert report["activated"] is True
    assert report["auc"] > report["baseline_auc"]
    model = ranker.active_model()
    assert model is not None
    assert len(model["weights"]) == len(ranker.FEATURE_NAMES)


def test_a_model_that_does_not_beat_the_baseline_stays_inactive(env):
    """Labels that follow the score perfectly: the baseline already wins."""
    from ops_dashboard.learning import ranker
    for i in range(70):
        q = f"pregunta sin relacion {i}"
        paths = [f"/tmp/ranker/score-{i}-{j}.md" for j in range(4)]
        for p in paths:
            _doc(p, "sin relacion")
        cites = [{"n": j + 1, "source": "obsidian", "path": p, "title": "x",
                  "score": 0.9 - j * 0.2} for j, p in enumerate(paths)]
        _turn(q, f"respuesta {i}", citations=cites)
        with __import__("ops_dashboard.db", fromlist=["db"]).db() as conn:
            mid = conn.execute(
                "SELECT id FROM messages WHERE role='assistant' ORDER BY id DESC LIMIT 1").fetchone()[0]
        # The clicked one is the first: exactly what the score already says.
        ranker.record_event(path=paths[0], source="obsidian", message_id=mid)
    report = ranker.train()
    assert report["trained"] is True
    assert report["activated"] is False
    assert "baseline" in report["reason"]
    assert ranker.active_model() is None


def test_rerank_keeps_the_order_when_there_is_no_model(env):
    from ops_dashboard.learning import ranker
    cands = [{"path": f"/tmp/x{j}.md", "score": 0.9 - j * 0.1, "title": "t"} for j in range(4)]
    out = ranker.rerank("q", cands)
    assert [c["path"] for c in out] == [c["path"] for c in cands]


def test_rerank_promotes_the_document_the_model_prefers(env):
    from ops_dashboard.learning import ranker
    for i, q, cites, good in _corpus():
        _turn(q, f"respuesta {i}", citations=cites)
        with __import__("ops_dashboard.db", fromlist=["db"]).db() as conn:
            mid = conn.execute(
                "SELECT id FROM messages WHERE role='assistant' ORDER BY id DESC LIMIT 1").fetchone()[0]
        ranker.record_event(path=good, source="obsidian", message_id=mid)
    assert ranker.train()["activated"] is True

    last = _corpus(1)[0]
    cands = [{"path": c["path"], "score": c["score"], "title": c["title"]} for c in last[2]]
    out = ranker.rerank(last[1], cands)
    assert out[0]["path"] == last[3]
    assert "rerank_score" in out[0]


def test_citations_carry_the_document_id(env):
    from ops_dashboard import brain
    p = _doc("/tmp/ranker/cite.md", "Para citar")
    ctx, cites = brain._format_context([{
        "doc_id": 42, "source": "obsidian", "path": p, "title": "Para citar",
        "score": 0.5, "content": "hola",
    }])
    assert cites[0]["doc_id"] == 42
    assert cites[0]["path"] == p


def test_the_stored_citations_are_readable_as_json(env):
    """The ranker counts citations with SQLite's json_each: it has to parse."""
    from ops_dashboard import db
    _doc("/tmp/ranker/j.md", "J")
    _turn("q", "a", citations=[{"n": 1, "source": "obsidian", "path": "/tmp/ranker/j.md",
                                 "title": "J", "score": 0.5}])
    with db.db() as conn:
        raw = conn.execute("SELECT citations FROM messages WHERE role='assistant'").fetchone()[0]
    assert json.loads(raw)[0]["path"] == "/tmp/ranker/j.md"


# ---------- the team chat teaches the ranker too ----------
# In the team chat the sources belong to the turn, and the click arrives on
# the agent's reply. The event has to be attached to the message that carries
# the citation list, or the training set never sees that turn.

def _team_turn(env, cites: list[dict]) -> tuple[int, int]:
    """A team turn: the user asks, the plan carries the sources, an agent answers."""
    from ops_dashboard import db
    with db.db() as conn:
        conn.execute("INSERT INTO conversations (title, created_at, updated_at) VALUES ('t', 1, 1)")
        conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) "
                     "VALUES (1, 'user', 'mi pregunta', 1)")
        conn.execute("INSERT INTO messages (conversation_id, role, content, citations, created_at) "
                     "VALUES (1, 'plan', 'el plan', ?, 2)", (json.dumps(cites),))
        conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) "
                     "VALUES (1, 'agent:m:1', 'respuesta del agente', 3)")
        plan = conn.execute("SELECT id FROM messages WHERE role='plan'").fetchone()[0]
        agent = conn.execute("SELECT id FROM messages WHERE role='agent:m:1'").fetchone()[0]
    return int(plan), int(agent)


def test_the_click_is_anchored_to_the_message_with_the_sources(env):
    from ops_dashboard.learning import ranker
    cites = [{"n": 1, "source": "obsidian", "path": "/tmp/ranker/t.md", "title": "T", "score": 0.5}]
    plan_mid, agent_mid = _team_turn(env, cites)
    # The agent's reply has no citations of its own: the anchor walks back.
    assert ranker.anchor_message(agent_mid) == plan_mid


def test_the_anchor_of_a_message_that_owns_its_sources_is_itself(env):
    from ops_dashboard.learning import ranker
    cites = [{"n": 1, "source": "obsidian", "path": "/tmp/ranker/t.md", "title": "T", "score": 0.5}]
    plan_mid, _agent_mid = _team_turn(env, cites)
    assert ranker.anchor_message(plan_mid) == plan_mid


def test_the_anchor_can_be_found_by_conversation_alone(env):
    from ops_dashboard.learning import ranker
    cites = [{"n": 1, "source": "obsidian", "path": "/tmp/ranker/t.md", "title": "T", "score": 0.5}]
    plan_mid, _agent_mid = _team_turn(env, cites)
    assert ranker.anchor_message(None, 1) == plan_mid


def test_a_team_turn_teaches_the_ranker(env):
    from ops_dashboard.learning import ranker
    cites = [{"n": 1, "source": "obsidian", "path": "/tmp/ranker/t.md", "title": "T", "score": 0.5}]
    plan_mid, _agent_mid = _team_turn(env, cites)
    rows = ranker.training_rows()
    assert len(rows) == 1
    assert rows[0]["label"] == 0
    assert rows[0]["question"] == "mi pregunta"
    ranker.record_event(path=cites[0]["path"], source="obsidian", message_id=plan_mid)
    rows = ranker.training_rows()
    assert rows[0]["label"] == 1


def test_the_team_chat_saves_the_sources_with_the_turn(env):
    from ops_dashboard import db, team_chat
    with db.db() as conn:
        conn.execute("INSERT INTO conversations (title, created_at, updated_at) VALUES ('t', 1, 1)")
    cites = [{"n": 1, "source": "obsidian", "path": "/tmp/ranker/t.md", "title": "T", "score": 0.5}]
    team_chat._save(1, "plan", "el plan", citations=cites)
    with db.db() as conn:
        raw = conn.execute("SELECT citations FROM messages WHERE role='plan'").fetchone()[0]
    assert json.loads(raw)[0]["path"] == "/tmp/ranker/t.md"
