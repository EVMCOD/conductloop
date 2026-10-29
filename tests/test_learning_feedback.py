"""Learning L3: 👍/👎 feedback (D7, always local) and the opt-in failure log
(D6: off by default, codes only, 30-day retention). Migration 9 (D15).

No network: the only socket is the in-process HTTP server on 127.0.0.1, and
provider failures are simulated with plain exceptions.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from posix_perms import assert_mode

from ops_dashboard import migrations

# ---------- migration 9 + gap fill ----------

def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_migration_9_creates_learning_tables():
    conn = sqlite3.connect(":memory:")
    applied = migrations.run_migrations(conn)
    assert 9 in [v for v, _ in applied]
    assert {"learning_settings", "feedback", "failure_log"} <= _tables(conn)
    assert migrations.current_version(conn) >= 9
    assert migrations.run_migrations(conn) == []  # idempotent


def test_migration_versions_unique_and_in_learning_range():
    versions = [v for v, _, _ in migrations.MIGRATIONS]
    assert len(versions) == len(set(versions))
    assert 9 in versions  # D15 reserves 9-12 for learning


def test_feedback_constraints():
    conn = sqlite3.connect(":memory:")
    migrations.run_migrations(conn)
    ins = ("INSERT INTO feedback (target_kind, target_id, rating, created_at, updated_at) "
           "VALUES (?, ?, ?, 0, 0)")
    conn.execute(ins, ("brain_message", 1, 1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("brain_message", 1, -1))  # one vote per target
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("brain_message", 2, 5))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("nope", 3, 1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO failure_log (kind, code, created_at) VALUES ('provider_error', '', 0)")


def test_gap_fill_applies_missing_8_without_lowering_version():
    """A DB that got 9 first (learning merged before sessions #18) must still
    get 8 later. Simulated with a stand-in idempotent migration 8."""
    conn = sqlite3.connect(":memory:")
    migrations.run_migrations(conn)
    has8 = any(v == 8 for v, _, _ in migrations.MIGRATIONS)
    calls: list[int] = []

    def fake_8(c):
        calls.append(8)
        c.execute("CREATE TABLE IF NOT EXISTS gap_probe (id INTEGER)")

    saved = list(migrations.MIGRATIONS)
    try:
        if has8:
            conn.execute("DELETE FROM schema_migrations WHERE version = 8")
            migrations.MIGRATIONS[:] = [m for m in saved if m[0] != 8] + [(8, "probe", fake_8)]
        else:
            migrations.MIGRATIONS.append((8, "probe", fake_8))
        before = migrations.current_version(conn)
        applied = migrations.run_migrations(conn)
        assert applied == [(8, "probe")]
        assert calls == [8]
        assert "gap_probe" in _tables(conn)
        assert migrations.current_version(conn) == before  # never lowered
        assert migrations.run_migrations(conn) == []  # now audited
    finally:
        migrations.MIGRATIONS[:] = saved


def test_gap_fill_never_reruns_old_migrations():
    conn = sqlite3.connect(":memory:")
    migrations.run_migrations(conn)
    conn.execute("DELETE FROM schema_migrations WHERE version < ?", (migrations.GAP_FILL_FROM,))
    assert migrations.run_migrations(conn) == []


# ---------- store ----------

@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "learn.db")
    db.init_db()
    return db


def _turn(answer: str, provider: str = "fake", model: str = "fake-1") -> int:
    from ops_dashboard import db_brain
    conv = db_brain.save_conversation_turn(title="t", user_content="q?", assistant_content=answer,
                                           provider=provider, model=model)
    from ops_dashboard import db
    with db.db() as conn:
        return int(conn.execute("SELECT id FROM messages WHERE conversation_id=? AND role='assistant'",
                                (conv,)).fetchone()[0])


def test_feedback_upsert_and_resolve_by_answer(tmpdb):
    from ops_dashboard import learning
    mid = _turn("La respuesta [1].")
    assert learning.resolve_brain_message(answer="La respuesta [1].") == mid
    assert learning.resolve_brain_message(answer="  La respuesta [1].\n") == mid
    with pytest.raises(learning.LearningError) as e:
        learning.resolve_brain_message(answer="otra")
    assert e.value.status == 404
    row = learning.record_feedback(target_kind="brain_message", target_id=mid, rating=-1,
                                   reason_code="bad_citation", comment="  cita mal  ")
    assert (row["rating"], row["reason_code"], row["comment"]) == (-1, "bad_citation", "cita mal")
    assert (row["provider"], row["model"]) == ("fake", "fake-1")
    row = learning.record_feedback(target_kind="brain_message", target_id=mid, rating=1,
                                   reason_code="wrong")
    assert row["rating"] == 1 and row["reason_code"] is None
    fb = learning.list_feedback()
    assert len(fb) == 1 and fb[0]["answer_excerpt"].startswith("La respuesta")
    assert learning.summary()["feedback"] == {"up": 1, "down": 0, "down_reasons": {}}
    assert learning.delete_feedback(fb[0]["id"]) and learning.list_feedback() == []


def test_feedback_resolves_newest_identical_answer(tmpdb):
    from ops_dashboard import learning
    _turn("igual")
    newer = _turn("igual")
    assert learning.resolve_brain_message(answer="igual") == newer


@pytest.mark.parametrize("kwargs,code", [
    ({"rating": 0}, "bad_rating"),
    ({"rating": True}, "bad_rating"),
    ({"rating": -1, "reason_code": "meh"}, "bad_reason"),
    ({"rating": 1, "target_kind": "session_task"}, "unsupported_target"),
])
def test_feedback_validation(tmpdb, kwargs, code):
    from ops_dashboard import learning
    mid = _turn("x")
    args = {"target_kind": "brain_message", "target_id": mid, **kwargs}
    with pytest.raises(learning.LearningError) as e:
        learning.record_feedback(**args)
    assert e.value.code == code


def test_failure_log_off_by_default_is_noop(tmpdb):
    from ops_dashboard import learning
    assert learning.failure_log_enabled() is False
    assert learning.record_failure("provider_error", "rate_limited", provider="openai") is False
    assert learning.list_failures() == []


def test_failure_log_codes_only_and_toggle_off_clears(tmpdb):
    from ops_dashboard import learning
    learning.set_failure_log_enabled(True)
    assert learning.record_failure("provider_error", "rate_limited", provider="openai", model="m")
    # Free text (could hold a question or a secret) is never stored as a code.
    assert learning.record_failure("tool_error", "Error: my secret question about sk-123")
    assert not learning.record_failure("not_a_kind", "x")
    codes = sorted(f["code"] for f in learning.list_failures())
    assert codes == ["other", "rate_limited"]
    cols = set(learning.list_failures()[0])
    assert cols == {"id", "kind", "code", "provider", "model", "route", "created_at"}
    learning.set_failure_log_enabled(False)
    assert learning.list_failures() == []


def test_failure_log_retention_30_days(tmpdb):
    from ops_dashboard import db, learning
    learning.set_failure_log_enabled(True)
    now = int(time.time())
    with db.db() as conn:
        conn.execute("INSERT INTO failure_log (kind, code, created_at) VALUES ('timeout', 'old', ?)",
                     (now - 31 * 86400,))
        conn.execute("INSERT INTO failure_log (kind, code, created_at) VALUES ('timeout', 'recent', ?)",
                     (now - 29 * 86400,))
    assert [f["code"] for f in learning.list_failures()] == ["recent"]
    with db.db() as conn:
        conn.execute("INSERT INTO failure_log (kind, code, created_at) VALUES ('timeout', 'old2', ?)",
                     (now - 40 * 86400,))
    learning.record_failure("timeout", "new")  # insert also purges
    assert sorted(f["code"] for f in learning.list_failures()) == ["new", "recent"]


def test_brain_provider_error_hook(tmpdb):
    from ops_dashboard import brain, learning
    err = brain._translate_provider_error(RuntimeError("429 Too Many Requests: my private prompt"), "openai")
    assert err.code  # classification unchanged
    assert learning.list_failures() == []  # off by default
    learning.set_failure_log_enabled(True)
    brain._translate_provider_error(TimeoutError("timed out"), "anthropic")
    brain._translate_provider_error(RuntimeError("429 Too Many Requests: my private prompt"), "openai")
    rows = learning.list_failures()
    assert {(r["kind"], r["provider"]) for r in rows} == {("timeout", "anthropic"), ("provider_error", "openai")}
    assert all("private" not in json.dumps(r) for r in rows)


def test_broken_citation_hook(tmpdb):
    from ops_dashboard import brain, learning

    class P:
        name = "fake"
        model = "fake-1"
    learning.set_failure_log_enabled(True)
    brain._log_broken_citations("ok [1]", [{"n": 1}], P())  # type: ignore[arg-type]
    assert learning.list_failures() == []
    brain._log_broken_citations("bad [3]", [{"n": 1}], P())  # type: ignore[arg-type]
    assert [(f["kind"], f["code"]) for f in learning.list_failures()] == [("broken_citation", "citation_out_of_range")]


def test_export_is_local_anonymised_and_private(tmpdb):
    from ops_dashboard import learning
    mid = _turn("respuesta secreta con datos")
    learning.record_feedback(target_kind="brain_message", target_id=mid, rating=-1, reason_code="wrong",
                             comment="comentario")
    path = learning.export_local()
    assert path.parent == learning.store.export_dir()
    assert_mode(path, 0o600)
    text = path.read_text()
    assert "respuesta secreta" not in text and "comentario" not in text
    data = json.loads(text)
    assert data["feedback"][0]["reason_code"] == "wrong"
    assert "comentario" in learning.export_local(include_comments=True).read_text()


# ---------- HTTP routes (real server, loopback only) ----------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server(tmpdb):
    from ops_dashboard.server import DashboardHandler

    class _Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _req(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if data else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_routes_end_to_end(server):
    from ops_dashboard import learning
    _turn("Respuesta del Brain [1]")
    st, hdr, body = _req(server + "/learning")
    assert st == 200 and b"Aprendizaje" in body
    assert "frame-ancestors 'none'" in hdr.get("Content-Security-Policy", "")
    for asset in ("/learning.js", "/learning.css", "/feedback-buttons.js"):
        assert _req(server + asset)[0] == 200, asset

    st, _, body = _req(server + "/api/learning/settings")
    assert st == 200 and json.loads(body)["failure_log_enabled"] is False

    st, _, body = _req(server + "/api/learning/feedback", "POST",
                       {"target_kind": "brain_message", "answer": "Respuesta del Brain [1]",
                        "rating": -1, "reason_code": "missing_citation", "comment": "falta"})
    assert st == 200, body
    fid = json.loads(body)["feedback"]["id"]
    st, _, body = _req(server + "/api/learning/feedback", "POST",
                       {"answer": "no existe", "rating": 1})
    assert st == 404 and json.loads(body)["error"] == "target_not_found"
    st, _, body = _req(server + "/api/learning/feedback", "POST", {"answer": "x", "rating": "1"})
    assert st == 400

    st, _, body = _req(server + "/api/learning/feedback?rating=-1")
    assert [f["id"] for f in json.loads(body)["feedback"]] == [fid]
    summ = json.loads(_req(server + "/api/learning/summary")[2])
    assert summ["feedback"]["down_reasons"] == {"missing_citation": 1}

    assert _req(server + "/api/learning/settings", "POST", {"failure_log_enabled": "yes"})[0] == 400
    assert _req(server + "/api/learning/settings", "POST", {"failure_log_enabled": True})[0] == 200
    learning.record_failure("provider_error", "auth_failed", provider="openai")
    fails = json.loads(_req(server + "/api/learning/failures")[2])
    assert fails["enabled"] and fails["retention_days"] == 30 and len(fails["failures"]) == 1
    assert json.loads(_req(server + "/api/learning/failures/clear", "POST", {})[2])["deleted"] == 1

    st, _, body = _req(server + "/api/learning/export", "POST", {})
    assert st == 200 and os.path.exists(json.loads(body)["path"])
    assert _req(server + "/api/learning/feedback/delete", "POST", {"id": fid})[0] == 200
    assert _req(server + "/api/learning/feedback/delete", "POST", {"id": fid})[0] == 404


@pytest.mark.parametrize("method,path", [
    ("GET", "/learning"), ("GET", "/api/learning/summary"), ("GET", "/api/learning/feedback"),
    ("GET", "/api/learning/failures"), ("POST", "/api/learning/settings"),
    ("POST", "/api/learning/feedback"), ("POST", "/api/learning/export"),
])
def test_routes_reject_relayed_requests(server, method, path):
    st, _, body = _req(server + path, method, {"failure_log_enabled": True} if method == "POST" else None,
                       headers={"X-Forwarded-For": "203.0.113.9"})
    assert st in (401, 403), (st, body)
    from ops_dashboard import learning
    assert learning.failure_log_enabled() is False


def test_local_only_decorator_403():
    from ops_dashboard import routes_learning

    class H:
        sent: list = []

        def _is_local(self):
            return False

        def _send(self, status, body, ctype, headers=None):
            self.sent.append((status, json.loads(body)))

    h = H()
    routes_learning.learning_summary(h)
    assert h.sent == [(403, {"ok": False, "error": "local_only",
                             "message": "learning data is only available on this Mac"})]


def test_store_makes_no_outbound_connections(tmpdb, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network used")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    from ops_dashboard import learning
    mid = _turn("y")
    learning.set_failure_log_enabled(True)
    learning.record_feedback(target_kind="brain_message", target_id=mid, rating=1)
    learning.record_failure("timeout", "t")
    learning.summary()
    learning.export_local()
