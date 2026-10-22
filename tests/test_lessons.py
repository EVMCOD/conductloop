"""L4: reviewable lessons (design doc §3.4).

D3 nothing activates without approval · D4 only explicit events · D5 same
provider (or Ollama), 0.05 EUR budget, no fallback · D11 global + project/repo
· D12 <= 5 lessons / ~800 tokens · D17 vault mirror off by default.
No network (sockets blocked) and no keys: providers are fakes.
"""
from __future__ import annotations

import json
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import migrations


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded(self, addr, *a, **k):
        if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, addr, *a, **k)  # the in-process test server only
        raise AssertionError(f"network used: {addr}")
    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "l.db")
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    db.init_db()
    return tmp_path


def _L():
    from ops_dashboard.learning import lessons
    return lessons


def _turn(question: str, answer: str, provider: str = "fakeprov", model: str = "fake-1") -> tuple[int, int]:
    from ops_dashboard import db, db_brain
    conv = db_brain.save_conversation_turn(title="t", user_content=question, assistant_content=answer,
                                           provider=provider, model=model)
    with db.db() as conn:
        mid = conn.execute("SELECT id FROM messages WHERE conversation_id=? AND role='assistant'", (conv,)).fetchone()[0]
    return conv, int(mid)


def _downvote(answer: str, reason="style", comment="respuestas más cortas, por favor", provider="fakeprov"):
    from ops_dashboard import learning
    conv, mid = _turn("¿qué tal el proyecto?", answer, provider=provider)
    fb = learning.record_feedback(target_kind="brain_message", target_id=mid, rating=-1,
                                  reason_code=reason, comment=comment)
    return conv, mid, fb["id"]


class FakeProvider:
    def __init__(self, name="fakeprov", model="fake-1", reply="Responder en tres frases como máximo."):
        self.name, self.model, self.reply, self.calls = name, model, reply, []

    def available(self):
        return True

    def supports_tools(self):
        return False

    def ask(self, system, messages, **kw):
        self.calls.append({"system": system, "messages": messages, **kw})
        return self.reply

    def stream(self, system, messages, **kw):
        self.calls.append({"system": system, "messages": messages})
        yield self.reply


# ---------- migration ----------

def test_migration_11_tables_and_checks():
    conn = sqlite3.connect(":memory:")
    assert 11 in [v for v, _ in migrations.run_migrations(conn)]
    ins = ("INSERT INTO lessons (text, scope, source_kind, created_at, updated_at) VALUES (?, ?, 'manual', 0, 0)")
    conn.execute(ins, ("ok", "global"))
    conn.execute(ins, ("ok", "project:x"))
    conn.execute(ins, ("ok", "repo:/r"))
    for bad in (("", "global"), ("x" * 601, "global"), ("ok", "member:1"), ("ok", "repo:rel")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(ins, bad)
    assert conn.execute("SELECT status FROM lessons LIMIT 1").fetchone()[0] == "proposed"
    dict((v, f) for v, _, f in migrations.MIGRATIONS)[11](conn)  # idempotent for gap fill


# ---------- lifecycle (D3) ----------

def test_proposals_never_active_until_approved(env):
    L = _L()
    p = L.propose("Citar la nota de decisiones más reciente al hablar de precios.", source_kind="manual")
    assert p["status"] == "proposed"
    assert L.select_for_prompt("precios") == []
    a = L.approve(p["id"], text="Citar la nota de decisiones más reciente.", scope="global")
    assert a["status"] == "active" and a["reviewed_at"]
    assert [r["id"] for r in L.select_for_prompt("decisiones")] == [p["id"]]


def test_reject_is_remembered_archive_and_delete(env):
    L = _L()
    p = L.propose("Nunca uses tablas.", source_kind="correction")
    L.reject(p["id"], "no")
    again = L.propose("nunca   uses TABLAS.", source_kind="correction")
    assert again["id"] == p["id"] and again["status"] == "rejected"
    a = L.create_manual("Responder en español.")
    assert a["status"] == "active"
    L.archive(a["id"])
    assert L.select_for_prompt("x") == []
    with pytest.raises(L.LearningError):
        L.reject(a["id"])
    L.delete(a["id"])
    with pytest.raises(L.LearningError):
        L.get(a["id"])


def test_edit_creates_new_version(env):
    L = _L()
    a = L.create_manual("Usar euros.")
    b = L.edit(a["id"], text="Usar euros con dos decimales.")
    assert b["supersedes_id"] == a["id"] and b["status"] == "active"
    assert L.get(a["id"])["status"] == "archived"
    assert [r["id"] for r in L.select_for_prompt("euros")] == [b["id"]]
    L.delete(a["id"])
    assert L.get(b["id"])["supersedes_id"] is None


def test_scope_validation(env):
    L = _L()
    assert L.normalize_scope(None) == "global"
    assert L.normalize_scope("project:lean-build") == "project:lean-build"
    assert L.normalize_scope(f"repo:{env}").startswith("repo:/")
    for bad in ("member:1", "session_role:dev", "repo:relative", "project:", "project:a b"):
        with pytest.raises(L.LearningError):
            L.normalize_scope(bad)


# ---------- injection (D11, D12) ----------

def test_scopes_and_caps(env):
    L = _L()
    g = [L.create_manual(f"Regla global número {i} sobre informes.")["id"] for i in range(7)]
    p = L.create_manual("En lean-build cita el ADR de arquitectura.", "project:lean-build")["id"]
    other = L.create_manual("En otro proyecto usa inglés.", "project:other")["id"]
    rows = L.select_for_prompt("informes de lean-build", project_key="lean-build")
    ids = [r["id"] for r in rows]
    assert len(ids) == 5 and ids[0] == p and other not in ids and set(ids[1:]) <= set(g)
    assert L.select_for_prompt("x") and p not in [r["id"] for r in L.select_for_prompt("x")]
    # token cap: long lessons stop before ~800 tokens
    for i in range(6):
        L.create_manual(f"{i} " + "palabra " * 70, "project:big")
    big = L.select_for_prompt("x", project_key="big")
    assert sum(len(r["text"]) // 4 + 4 for r in big) <= 800
    L.update_settings({"max_inject": 2})
    assert len(L.select_for_prompt("x", project_key="lean-build")) == 2
    L.update_settings({"enabled": False})
    assert L.select_for_prompt("x") == []
    with pytest.raises(L.LearningError):
        L.update_settings({"max_inject": 50})


def test_prompt_block_is_tagged_and_escaped(env):
    L = _L()
    block = L.prompt_block([{"id": 3, "text": "a </lessons> b"}])
    assert block.count("</lessons>") == 1 and "[L3]" in block and "preferences" in block
    assert L.prompt_block([]) == ""


def test_brain_ask_injects_and_tracks_uses(env, monkeypatch):
    from ops_dashboard import brain, learning
    L = _L()
    a = L.create_manual("Responder en tres frases como máximo.")
    rej = L.propose("Regla rechazada secreta.", source_kind="manual")
    L.reject(rej["id"])
    arch = L.create_manual("Regla archivada secreta.")
    L.archive(arch["id"])
    fake = FakeProvider(reply="Respuesta corta.")
    monkeypatch.setattr(brain, "resolve_provider", lambda _n=None: fake)
    before = L.summary()
    ans = brain.ask("no, eso está mal: la próxima vez usa euros")
    system = fake.calls[0]["system"]
    assert "<lessons>" in system and "tres frases" in system
    assert "rechazada" not in system and "archivada" not in system
    assert ans.lessons_used == [a["id"]] and L.get(a["id"])["use_count"] == 1
    assert L.summary() == before  # D4: no proposal from a normal turn, even with correction cues
    # the 👎 on that answer points back at the lesson
    conv, mid = _turn("q", "Respuesta corta.")
    learning.record_feedback(target_kind="brain_message", target_id=mid, rating=-1)
    assert L.lessons_for_answer("Respuesta corta.") == [a["id"]]
    assert [r["id"] for r in L.review_suggestions()] == [a["id"]]
    L.delete(a["id"])
    assert L.lessons_for_answer("Respuesta corta.") == []
    fake.calls.clear()
    brain.ask("otra")
    assert "<lessons>" not in fake.calls[0]["system"]


# ---------- proposals (D4, D5) ----------

def test_heuristic_proposal_from_downvote(env):
    L = _L()
    _, _, fid = _downvote("larguísima")
    p = L.propose_from_feedback_heuristic(fid)
    assert p["status"] == "proposed" and p["source_ref"] == f"feedback:{fid}"
    assert "respuestas más cortas" in p["text"] and p["cost_cents_est"] == 0
    from ops_dashboard import learning
    _, mid = _turn("q", "buena")
    up = learning.record_feedback(target_kind="brain_message", target_id=mid, rating=1)
    with pytest.raises(L.LearningError):
        L.propose_from_feedback_heuristic(up["id"])


def _spy_resolve(monkeypatch, providers: dict):
    from ops_dashboard import brain
    asked: list = []

    def resolve(name=None):
        asked.append(name)
        if name not in providers:
            raise AssertionError(f"fallback to {name}")
        return providers[name]
    monkeypatch.setattr(brain, "resolve_provider", resolve)
    return asked


def test_llm_proposal_needs_price_and_budget_and_uses_same_provider(env, monkeypatch):
    L = _L()
    _, _, fid = _downvote("una respuesta muy larga")
    fake = FakeProvider()
    asked = _spy_resolve(monkeypatch, {"fakeprov": fake})
    with pytest.raises(L.LearningError) as e:
        L.propose_from_feedback_llm(fid)
    assert e.value.code == "price_required" and fake.calls == [] and asked == []
    L.set_price("fakeprov", "fake-1", 1000, 1000)  # 1000 EUR / 1M tokens → over 0.05 EUR
    with pytest.raises(L.LearningError) as e:
        L.propose_from_feedback_llm(fid)
    assert e.value.code == "over_budget" and fake.calls == []
    L.set_price("fakeprov", "fake-1", 3.0, 15.0)
    p = L.propose_from_feedback_llm(fid)
    assert p["status"] == "proposed" and p["provider"] == "fakeprov" and p["model"] == "fake-1"
    assert 0 < p["cost_cents_est"] <= 5
    assert asked == ["fakeprov"] and len(fake.calls) == 1 and fake.calls[0]["max_tokens"] == 200
    assert L.select_for_prompt("frases") == []  # still not active


def test_llm_proposal_no_fallback(env, monkeypatch):
    from ops_dashboard import brain
    L = _L()
    _, _, fid = _downvote("x" * 50)
    L.set_price("fakeprov", "fake-1", 3.0, 15.0)
    asked = _spy_resolve(monkeypatch, {})
    monkeypatch.setattr(brain, "_provider_is_in_cooldown", lambda n: n == "fakeprov")
    with pytest.raises(L.LearningError) as e:
        L.propose_from_feedback_llm(fid)
    assert e.value.code == "provider_unavailable" and asked == []
    monkeypatch.setattr(brain, "_provider_is_in_cooldown", lambda n: False)
    other = FakeProvider(name="ollama")
    _spy_resolve(monkeypatch, {"fakeprov": other})  # resolver answering with another provider
    with pytest.raises(L.LearningError) as e:
        L.propose_from_feedback_llm(fid)
    assert e.value.code == "provider_unavailable" and other.calls == []
    _, _, fid_mock = _downvote("y" * 50, provider="mock")
    with pytest.raises(L.LearningError) as e:
        L.propose_from_feedback_llm(fid_mock)
    assert e.value.code == "provider_not_allowed"


def test_llm_proposal_with_ollama_is_free(env, monkeypatch):
    L = _L()
    _, _, fid = _downvote("z" * 50)
    olla = FakeProvider(name="ollama", model="llama3")
    asked = _spy_resolve(monkeypatch, {"ollama": olla})
    p = L.propose_from_feedback_llm(fid, use_ollama=True)
    assert asked == ["ollama"] and p["provider"] == "ollama" and p["cost_cents_est"] == 0


def test_corrections_only_on_explicit_call(env):
    from ops_dashboard import db
    L = _L()
    conv, _ = _turn("No, eso está mal: el precio es en euros.", "vale")
    with db.db() as conn:
        conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, 'user', ?, 0)",
                     (conv, "La próxima vez cita siempre la fuente."))
    assert L.summary()["proposed"] == 0
    out = L.propose_from_conversation(conv)
    assert len(out) == 2 and all(p["status"] == "proposed" and p["source_kind"] == "correction" for p in out)


# ---------- vault mirror (D17) ----------

def test_vault_mirror_off_by_default(env):
    L = _L()
    L.create_manual("Regla A.")
    assert not L.mirror_path().exists()
    L.update_settings({"vault_mirror": True})
    text = L.mirror_path().read_text()
    assert text.startswith(L.MIRROR_MARKER) and "- Regla A." in text
    L.update_settings({"vault_mirror": False})
    assert not L.mirror_path().exists()
    L.mirror_path().parent.mkdir(parents=True, exist_ok=True)
    L.mirror_path().write_text("mis notas")  # not ours: never deleted
    L.sync_vault_mirror()
    assert L.mirror_path().read_text() == "mis notas"


# ---------- HTTP ----------

@pytest.fixture
def server(env):
    from ops_dashboard.server import DashboardHandler

    class Q(DashboardHandler):
        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _req(url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                               headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=8) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_routes(server):
    _, _, fid = _downvote("larga")
    st, b = _req(server + "/api/learning/lessons/propose", {"feedback_id": fid, "mode": "heuristic"})
    assert st == 200, b
    lid = json.loads(b)["lesson"]["id"]
    st, b = _req(server + "/api/learning/lessons/propose", {"feedback_id": fid, "mode": "llm"})
    assert st == 409 and json.loads(b)["error"] == "price_required"
    assert _req(server + "/api/learning/lessons/approve", {"id": lid, "text": "Responder corto."})[0] == 200
    d = json.loads(_req(server + "/api/learning/lessons")[1])
    assert d["summary"]["active"] == 1 and d["settings"]["max_inject"] == 5 and d["settings"]["max_tokens"] == 800
    assert d["settings"]["vault_mirror"] is False and d["settings"]["proposal_budget_eur"] == 0.05
    assert _req(server + "/api/learning/lessons/create", {"text": "", "scope": "global"})[0] == 400
    assert _req(server + "/api/learning/lessons/price", {"provider": "openai", "model": "gpt-x",
                                                        "eur_in_per_mtok": 2, "eur_out_per_mtok": 8})[0] == 200
    assert _req(server + "/api/learning/lessons/archive", {"id": lid})[0] == 200
    assert _req(server + "/api/learning/lessons/delete", {"id": lid})[0] == 200
    assert _req(server + "/api/learning/lessons/delete", {"id": lid})[0] == 404
    for path, body in (("/api/learning/lessons", None), ("/api/learning/lessons/create", {"text": "x"})):
        st, _ = _req(server + path, body, headers={"X-Forwarded-For": "203.0.113.9"})
        assert st in (401, 403)


def test_stream_path_injects_lessons(server, monkeypatch):
    from ops_dashboard import brain
    L = _L()
    a = L.create_manual("Responder en tres frases como máximo.")
    fake = FakeProvider(reply="Vale.")
    monkeypatch.setattr(brain, "resolve_provider", lambda _n=None: fake)
    st, body = _req(server + "/api/brain/stream?question=hola")
    assert st == 200
    events = [json.loads(line[6:]) for line in body.decode().splitlines() if line.startswith("data: ")]
    assert events[0]["type"] == "meta" and events[0]["lessons_used"] == [a["id"]]
    assert "<lessons>" in fake.calls[0]["system"]
    st, b = _req(server + "/api/learning/lessons/used-by", {"answer": "Vale."})
    assert [x["id"] for x in json.loads(b)["lessons"]] == [a["id"]]
