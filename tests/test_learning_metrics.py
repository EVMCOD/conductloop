"""L8: "¿estoy aprendiendo bien?" metrics, optional local A/B and the
scheduled curator (design doc §7, D14). Synthetic data with known answers;
no network, no keys."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ops_dashboard import migrations

NOW = 1_800_000_000
DAY = 86400


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded(self, addr, *a, **k):
        if isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, addr, *a, **k)
        raise AssertionError(f"network used: {addr}")
    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "l.db")
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("OPS_EVALS_DIR", str(tmp_path / "no-evals"))
    monkeypatch.delenv("OPS_SKILLS_DIR", raising=False)
    (tmp_path / "vault" / "skills").mkdir(parents=True)
    db.init_db()
    return tmp_path


def _sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()


def _answers(n: int, at: int, *, down: int, lessons: bool = False, arm: str | None = None, tag: str = "a") -> None:
    """n assistant answers created at ``at``; the first ``down`` get a 👎."""
    from ops_dashboard import db
    with db.db() as conn:
        conv = conn.execute("INSERT INTO conversations (title, created_at, updated_at) VALUES ('t', ?, ?)",
                            (at, at)).lastrowid
        for i in range(n):
            text = f"respuesta {tag}-{i}"
            mid = conn.execute("INSERT INTO messages (conversation_id, role, content, provider, model, created_at) "
                               "VALUES (?, 'assistant', ?, 'p', 'm', ?)", (conv, text, at)).lastrowid
            if i < down:
                conn.execute("INSERT INTO feedback (target_kind, target_id, rating, created_at, updated_at) "
                             "VALUES ('brain_message', ?, -1, ?, ?)", (mid, at, at))
            if lessons:
                conn.execute("INSERT INTO lesson_uses (answer_sha, lesson_id, created_at) VALUES (?, 1, ?)",
                             (_sha(text), at))
            if arm:
                conn.execute("INSERT INTO learning_ab (answer_sha, arm, created_at) VALUES (?, ?, ?)",
                             (_sha(text), arm, at))


def test_migration_13_and_idempotent():
    conn = sqlite3.connect(":memory:")
    assert 13 in [v for v, _ in migrations.run_migrations(conn)]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO learning_ab (answer_sha, arm, created_at) VALUES (?, 'other', 0)", ("a" * 64,))
    migrations._mig_13_learning_ab(conn)


def test_downvote_rates_match_synthetic_data(env):
    from ops_dashboard.learning import metrics as M
    _answers(20, NOW - 2 * DAY, down=2, lessons=True, tag="with")      # 10 / 100
    _answers(20, NOW - 3 * DAY, down=6, tag="without")                 # 30 / 100
    _answers(25, NOW - 40 * DAY, down=10, tag="prev")                  # previous window: 40 / 100
    r = M.report(30, now=NOW)
    dv = r["downvotes"]
    assert dv["all"] == {"answers": 40, "down": 8, "per_100": 20.0, "enough": True}
    assert dv["with_lessons"] == {"answers": 20, "down": 2, "per_100": 10.0, "enough": True}
    assert dv["without_lessons"] == {"answers": 20, "down": 6, "per_100": 30.0, "enough": True}
    assert r["previous"]["all"] == {"answers": 25, "down": 10, "per_100": 40.0, "enough": True}
    sig = {s["code"]: s for s in r["signals"]}
    assert sig["downvote_trend"]["value"] == "better" and sig["downvote_trend"]["before"] == 40.0
    assert sig["lessons_effect"]["value"] == "helps"
    assert "ab_effect" not in sig  # A/B off and no holdout data


def test_small_samples_are_low_signal(env):
    from ops_dashboard.learning import metrics as M
    _answers(5, NOW - DAY, down=5, tag="few")
    r = M.report(30, now=NOW)
    assert r["downvotes"]["all"]["per_100"] == 100.0 and r["downvotes"]["all"]["enough"] is False
    assert {s["code"]: s["value"] for s in r["signals"]}["downvote_trend"] == "low_signal"


def test_ab_arms_match_synthetic_data(env):
    from ops_dashboard.learning import metrics as M
    _answers(20, NOW - DAY, down=1, lessons=True, arm="lessons", tag="l")   # 5 / 100
    _answers(20, NOW - DAY, down=6, arm="holdout", tag="h")                 # 30 / 100
    r = M.report(30, now=NOW)
    ab = r["downvotes"]["ab"]
    assert ab["lessons"]["per_100"] == 5.0 and ab["holdout"]["per_100"] == 30.0
    assert {s["code"]: s["value"] for s in r["signals"]}["ab_effect"] == "helps"


def test_identical_answers_keep_their_own_arm(env):
    # Deterministic answers repeat word for word: two answers with the same text
    # in different arms must not collapse into one arm (Codex on #39).
    from ops_dashboard import db
    from ops_dashboard.learning import metrics as M
    with db.db() as conn:
        conv = conn.execute("INSERT INTO conversations (title, created_at, updated_at) VALUES ('t', ?, ?)",
                            (NOW - DAY, NOW - DAY)).lastrowid
        for k, arm in enumerate(["lessons", "holdout"] * 10):
            at = NOW - DAY + k
            mid = conn.execute("INSERT INTO messages (conversation_id, role, content, provider, model, created_at) "
                               "VALUES (?, 'assistant', 'misma respuesta', 'p', 'm', ?)", (conv, at)).lastrowid
            conn.execute("INSERT INTO learning_ab (answer_sha, arm, created_at) VALUES (?, ?, ?)",
                         (_sha("misma respuesta"), arm, at))
            if arm == "lessons":
                conn.execute("INSERT INTO lesson_uses (answer_sha, lesson_id, created_at) VALUES (?, 1, ?)",
                             (_sha("misma respuesta"), at))
            if arm == "holdout" and k < 6:
                conn.execute("INSERT INTO feedback (target_kind, target_id, rating, created_at, updated_at) "
                             "VALUES ('brain_message', ?, -1, ?, ?)", (mid, at, at))
    d = M.report(30, now=NOW)["downvotes"]
    assert d["ab"]["lessons"]["answers"] == 10 and d["ab"]["holdout"]["answers"] == 10
    assert d["ab"]["lessons"]["down"] == 0 and d["ab"]["holdout"]["down"] == 3
    assert d["with_lessons"]["answers"] == 10 and d["without_lessons"]["answers"] == 10


def test_lessons_to_review_looks_beyond_the_top_20(env):
    from ops_dashboard import db
    from ops_dashboard.learning import lessons as L
    from ops_dashboard.learning import metrics as M
    popular = [L.create_manual(f"Lección popular {i}.")["id"] for i in range(25)]   # no 👎…
    new = L.create_manual("Lección nueva y mala.")["id"]     # …and a new one that keeps failing
    with db.db() as conn:
        conn.executemany("UPDATE lessons SET use_count = 100 WHERE id = ?", [(i,) for i in popular])
        conn.execute("UPDATE lessons SET use_count = 3 WHERE id = ?", (new,))
    now = int(time.time())
    _answers(3, now - DAY, down=3, tag="nueva")
    with db.db() as conn:
        conn.executemany("INSERT INTO lesson_uses (answer_sha, lesson_id, created_at) VALUES (?, ?, ?)",
                         [(_sha(f"respuesta nueva-{i}"), new, now - DAY) for i in range(3)])
    r = M.report(30)
    sig = {s["code"]: s for s in r["signals"]}
    assert sig["lessons_to_review"]["ids"] == [new]
    assert len(r["lessons"]["top"]) == 20 and "_all" not in r["lessons"]


def test_ab_off_by_default_and_holdout_skips_lessons(env, monkeypatch):
    from ops_dashboard import db
    from ops_dashboard.learning import lessons as L
    from ops_dashboard.learning import metrics as M
    assert M.settings() == {"ab_holdout_pct": 0, "ab_max_pct": 50, "curator_enabled": True}
    L.create_manual("Dar los precios en euros.")
    monkeypatch.setattr(M.random, "random", lambda: 0.0)  # would always hold out if enabled
    system, ids = L.apply_to_system("SYS", "precios")
    assert "<lessons>" in system and ids and not isinstance(ids, L.HoldoutIds)
    L.mark_used(ids, "respuesta 1")
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM learning_ab").fetchone()[0] == 0  # nothing recorded when off
    M.update_settings({"ab_holdout_pct": 50})
    system, ids = L.apply_to_system("SYS", "precios")
    assert system == "SYS" and isinstance(ids, L.HoldoutIds) and list(ids) == [] and ids.eligible
    L.mark_used(ids, "respuesta 2")
    monkeypatch.setattr(M.random, "random", lambda: 0.99)
    system, ids = L.apply_to_system("SYS", "precios")
    assert "<lessons>" in system and ids
    L.mark_used(ids, "respuesta 3")
    with db.db() as conn:
        arms = dict(conn.execute("SELECT answer_sha, arm FROM learning_ab").fetchall())
        uses = conn.execute("SELECT use_count FROM lessons").fetchone()[0]
    assert arms == {_sha("respuesta 2"): "holdout", _sha("respuesta 3"): "lessons"}
    assert uses == 2  # the holdout answer did not count as a use
    for bad in (-1, 51, "10", True):
        with pytest.raises(M.LearningError):
            M.update_settings({"ab_holdout_pct": bad})


def test_lessons_skills_sessions_blocks(env):
    from ops_dashboard import db
    from ops_dashboard.learning import lessons as L
    from ops_dashboard.learning import metrics as M
    from ops_dashboard.skills import propose as P
    ids = [L.create_manual(f"Regla {i}.")["id"] for i in range(4)]
    L.archive(ids[0])
    L.propose("Propuesta.", source_kind="manual")
    good = "---\nname: {n}\ndescription: Skill {n} de prueba.\n---\n\nPasos.\n"
    for n in ("uno", "dos", "tres"):
        P.approve(P.create("create", n, content=good.format(n=n), source_kind="manual")["id"])
    P.reject(P.create("create", "cuatro", content=good.format(n="cuatro"), source_kind="manual")["id"])
    r = M.report(30)
    assert r["lessons"]["counts"] == {"proposed": 1, "active": 3, "rejected": 0, "archived": 1}
    assert r["lessons"]["archived_pct"] == 25.0
    sk = r["skills"]
    assert sk["indexed"] == 3 and sk["proposals"]["approved"] == 3 and sk["approval_pct"] == 75.0
    assert sk["with_without_delta"] is None
    # sessions (#18) are another stack: computed from (attempts, created_at) of done tasks
    first = 1000
    rows = [(1, first - 10)] * 4 + [(2, first - 10)] * 4 + [(1, first + 10)] * 3 + [(3, first + 10)]
    ss = M.sessions_from_rows(rows, first)
    assert ss["all"] == {"done": 12, "first_try": 7, "first_try_pct": 58.3, "enough": False}
    assert ss["before_lessons"]["first_try_pct"] == 50.0 and ss["after_lessons"]["first_try_pct"] == 75.0
    with db.db() as conn:
        has_sessions = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'session_tasks'").fetchone()
    assert r["sessions"]["available"] is bool(has_sessions)
    assert r["evals"]["available"] is False


def test_lessons_to_review_signal(env):
    from ops_dashboard import db
    from ops_dashboard.learning import lessons as L
    from ops_dashboard.learning import metrics as M
    lid = L.create_manual("Siempre en inglés.")["id"]
    now = int(time.time())
    _answers(3, now - DAY, down=3, tag="bad")
    with db.db() as conn:
        conn.executemany("INSERT INTO lesson_uses (answer_sha, lesson_id, created_at) VALUES (?, ?, ?)",
                         [(_sha(f"respuesta bad-{i}"), lid, now - DAY) for i in range(3)])
    sig = {s["code"]: s for s in M.report(30)["signals"]}
    assert sig["lessons_to_review"]["ids"] == [lid]


# ---------- curator (D14): scheduled, only proposes, never deletes ----------

def _skill(env, name: str, age_days: int) -> Path:
    d = env / "vault" / "skills" / name
    d.mkdir()
    f = d / "SKILL.md"
    f.write_text(f"---\nname: {name}\ndescription: Skill {name}.\n---\nCuerpo.\n")
    t = time.time() - age_days * DAY
    os.utime(f, (t, t))
    return f


def test_scheduled_curator_only_proposes(env):
    from ops_dashboard import scheduler
    from ops_dashboard.indexers import skills as idx
    from ops_dashboard.learning import metrics as M
    from ops_dashboard.skills import propose as P
    old, fresh = _skill(env, "vieja", 61), _skill(env, "nueva", 5)
    idx.run()
    before = sorted(str(p) for p in (env / "vault").rglob("*"))

    def boom(*a, **k):
        raise AssertionError("the curator must not move or delete files")
    with pytest.MonkeyPatch.context() as mp:
        for mod, name in ((shutil, "rmtree"), (shutil, "move"), (os, "remove"), (os, "unlink"), (os, "rename"),
                          (os, "replace"), (Path, "unlink"), (Path, "rename"), (Path, "rmdir")):
            mp.setattr(mod, name, boom)
        ran = scheduler.run_system_jobs(datetime(2026, 7, 21, 4, 15))
    assert {"system_job": "learning_maintenance", "ok": True} in ran
    assert sorted(str(p) for p in (env / "vault").rglob("*")) == before
    assert old.is_file() and fresh.is_file()
    pend = P.list_proposals("pending")
    assert [(p["action"], p["skill_name"], p["source_kind"]) for p in pend] == [("archive", "vieja", "curator")]
    # again the next day: no duplicate
    assert M.scheduled_maintenance()["proposed"] == []
    # disabled → nothing
    M.update_settings({"curator_enabled": False})
    P.reject(pend[0]["id"])
    assert M.scheduled_maintenance(now=int(time.time()) + 70 * DAY)["curator"] == "disabled"
    assert P.summary()["pending"] == 0


def test_scheduled_job_not_at_other_times(env):
    from ops_dashboard import scheduler
    scheduler._sys_fired.clear()
    ran = {r["system_job"] for r in scheduler.run_system_jobs(datetime(2026, 7, 21, 9, 17))}
    assert "learning_maintenance" not in ran


# ---------- HTTP + UI strings ----------

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
    st, b = _req(server + "/api/learning/metrics?days=7")
    assert st == 200 and json.loads(b)["days"] == 7
    assert _req(server + "/api/learning/metrics?days=0")[0] == 400
    assert _req(server + "/api/learning/metrics?days=abc")[0] == 400
    st, b = _req(server + "/api/learning/metrics/settings", {"ab_holdout_pct": 10, "curator_enabled": False})
    assert st == 200 and json.loads(b)["settings"] == {"ab_holdout_pct": 10, "ab_max_pct": 50, "curator_enabled": False}
    assert _req(server + "/api/learning/metrics/settings", {"ab_holdout_pct": 99})[0] == 400
    for path, body in (("/api/learning/metrics", None), ("/api/learning/metrics/settings", {"ab_holdout_pct": 0})):
        st, _ = _req(server + path, body, headers={"X-Forwarded-For": "203.0.113.9"})
        assert st in (401, 403)


def test_every_signal_has_es_and_en_text():
    js = (Path(__file__).resolve().parent.parent / "ops_dashboard" / "web" / "learning.js").read_text()
    combos = [f"s_{c}_{v}" for c in ("downvote_trend",) for v in ("better", "worse", "flat", "low_signal")]
    combos += [f"s_{c}_{v}" for c in ("lessons_effect", "ab_effect") for v in ("helps", "hurts", "no_difference",
                                                                              "low_signal")]
    combos += ["s_lessons_to_review", "s_unused_skills"]
    for k in combos:
        assert len(re.findall(rf"\b{k}:\s*'", js)) == 2, k
