"""Write-path coverage for the extracted db_* domain modules — the create/
update logic the read-only smoke sweep never exercises. Lifts coverage and
guards the refactor's write paths against regressions.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "w.db"))
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "w.db"  # type: ignore[attr-defined]
    _db.init_db()
    return _db


def test_task_run_lifecycle(db):
    tid = db.create_task(None, "Test task", "do the thing")
    assert db.get_task(tid)["title"] == "Test task"
    rid = db.start_run(tid, provider="ollama", model="llama3")
    db.finish_run(rid, status="success", output_text="done")
    run = db.get_run(rid)
    assert run["status"] == "success"
    assert any(t["id"] == tid for t in db.list_tasks())
    assert db.task_stats()["total"] >= 1


def test_revenue_write_and_summary(db):
    sid = db.add_revenue_source("Stripe", "stripe", "proj")
    db.insert_revenue_event(
        source_id=sid, source_type="stripe", project_key="proj",
        external_id="evt_1", event_type="charge", amount_cents=1500,
        currency="USD", occurred_at=int(time.time()),
    )
    assert len(db.list_revenue_events()) == 1
    summ = db.revenue_summary()
    assert summ["total_cents"] >= 1500


def test_agent_memory_and_triggers(db):
    mid = db.add_agent_memory(1, "remembered fact", importance=8)
    assert mid > 0
    mem = db.list_agent_memory(1)
    assert any(m["content"] == "remembered fact" for m in mem)
    trig_id = db.add_trigger(1, "cron", "0 9 * * *", prompt="morning brief")
    assert any(t["id"] == trig_id for t in db.list_triggers(1))


def test_costs_write_and_summary(db):
    db.add_cost("openai", 250, project_key="proj", category="llm")
    db.add_cost("cloudflare", 100, project_key="proj", category="infra")
    assert len(db.list_costs()) == 2
    summ = db.cost_summary()
    assert summ["total_cents"] == 350


def test_decisions_write_read(db):
    did = db.add_decision("Use SQLite", "Local-first, no server DB",
                          project_key="proj", tags=["arch"])
    assert db.get_decision(did)["title"] == "Use SQLite"
    assert any(d["id"] == did for d in db.list_decisions())
    # alias path (create_decision -> add_decision) still works post-split
    did2 = db.create_decision("Second", "another call", project_key="proj")
    assert db.get_decision(did2) is not None
