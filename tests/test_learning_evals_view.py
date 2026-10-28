"""L7: read-only Evals panel. Reads evals/*.json + baseline.json; never
imports or runs the harness; no network."""
from __future__ import annotations

import json
import sys

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "l.db")
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    db.init_db()
    return tmp_path


def _write_suite(root, name, cases):
    d = root / name
    d.mkdir(parents=True)
    (d / "evals.json").write_text(json.dumps({"conductloop": {"suite": name, "target": "brain.ask"},
                                              "evals": cases}))


def test_missing_dir_is_unavailable(env, monkeypatch):
    from ops_dashboard.learning import evals_view as E
    monkeypatch.setenv("OPS_EVALS_DIR", str(env / "nope"))
    d = E.overview()
    assert d["available"] is False and d["suites"] == [] and d["read_only"] is True


def test_reads_suites_and_baseline_without_importing_harness(env, monkeypatch):
    from ops_dashboard.learning import evals_view as E
    root = env / "evals"
    _write_suite(root, "brain-rag", [
        {"id": 1, "prompt": "<img src=x onerror=alert(1)>" + "x" * 500,
         "conductloop": {"key": "rag-a", "required": True, "checks": [{"type": "must_cite"}, {"type": "must_cite"}]}},
        {"id": 2, "prompt": "b", "conductloop": {"key": "rag-b", "checks": [{"type": "must_contain"}]}},
        "basura",
    ])
    (root / "broken").mkdir()
    (root / "broken" / "evals.json").write_text("{no json")
    (root / "baseline.json").write_text(json.dumps({
        "policy": "fail if any required case fails", "overall": {"passed": 1, "total": 2, "score": 50.0},
        "suites": {"brain-rag": {"passed": 1, "total": 2, "score": 50.0}},
        "cases": {"brain-rag/rag-a": True, "brain-rag/rag-b": False}}))
    monkeypatch.setenv("OPS_EVALS_DIR", str(root))
    before = {m for m in sys.modules if m.startswith("ops_dashboard.evals")}
    d = E.overview()
    assert {m for m in sys.modules if m.startswith("ops_dashboard.evals")} == before
    assert d["available"] and d["overall"] == {"passed": 1, "total": 2, "score": 50.0}
    [s] = d["suites"]
    assert s["suite"] == "brain-rag" and s["required"] == 1 and len(s["cases"]) == 2
    a, b = s["cases"]
    assert a["checks"] == ["must_cite"] and a["baseline_passed"] is True and len(a["prompt"]) == E.MAX_PROMPT
    assert b["baseline_passed"] is False and b["required"] is False
    assert s["baseline"] == {"passed": 1, "total": 2, "score": 50.0}


def test_local_runs_only_if_l2_table_exists(env):
    from ops_dashboard import db
    from ops_dashboard.learning import evals_view as E
    assert E.local_runs() == []  # L2 table (migration 25) exists but is empty
    with db.db() as conn:
        conn.execute("DROP TABLE eval_results")
        conn.execute("DROP TABLE eval_runs")
    assert E.local_runs() == []  # and a DB without the table is fine too
    with db.db() as conn:
        conn.execute("CREATE TABLE eval_runs (id INTEGER PRIMARY KEY, mode TEXT, suite TEXT, provider TEXT, "
                     "key_ref TEXT, passed INTEGER, failed INTEGER, started_at INTEGER)")
        conn.execute("INSERT INTO eval_runs VALUES (1, 'live', 'brain-rag', 'ollama', 'byok.x', 3, 1, 100)")
    [r] = E.local_runs()
    assert r["suite"] == "brain-rag" and r["passed"] == 3 and "key_ref" not in r  # never exposes key refs
