"""The "Now" view — what needs the user, in one answer.

The app spreads the work of a day over a dozen views: approvals waiting for a
decision, agent tasks that stopped, todos that fell due, signals from the
numbers. ``ops_dashboard/now.py`` gathers the pieces that ask for the person
into one ordered answer so the app can open on it.

Two things are checked here: that the answer holds the right things in the
right order, and that it never depends on anything slow or breakable — it
must not call the model and it must not write.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ops_dashboard import db, now


def _day(offset_days: int = 0) -> int:
    """The unix timestamp of the start of a day, relative to today."""
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int((start + timedelta(days=offset_days)).timestamp())


@pytest.fixture(autouse=True)
def _own_database(tmp_path, monkeypatch):
    """Each test gets its own database: the counts below are exact."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "now.db")
    db.init_db(backup_before_upgrade=False)


def _sections(answer: dict) -> dict:
    return {s["id"]: s for s in answer["sections"]}


# --- the shape of the answer -------------------------------------------------


def test_an_empty_workspace_has_nothing_waiting():
    answer = now.build()
    assert answer["ok"] is True
    assert answer["needs_you"] == 0
    assert [s["id"] for s in answer["sections"]] == ["blocking", "today", "signals", "context"]


def test_every_item_says_where_it_lives():
    """Each item points at the view it came from, so the panel can send the
    person straight there instead of making them look for it."""
    db.add_approval(1, "publish_release", {"note": "ship it"}, reason="publish 1.2.16")
    db.add_user_todo("call the bank", due_date=_day())
    answer = now.build()
    items = [i for s in answer["sections"] for i in s["items"]]
    assert items
    for item in items:
        assert item["view"]
        assert item["id"]
        assert item["severity"] in (now.BLOCKING, now.DUE, now.SIGNAL, now.CONTEXT)


def test_the_item_ids_are_unique():
    db.add_approval(1, "a", {})
    db.add_approval(1, "b", {})
    db.add_user_todo("one", due_date=_day())
    db.add_user_todo("two", due_date=_day())
    answer = now.build()
    ids = [i["id"] for s in answer["sections"] for i in s["items"]]
    assert len(ids) == len(set(ids))


# --- what blocks the person --------------------------------------------------


def test_a_pending_approval_is_something_that_needs_you():
    db.add_approval(1, "publish_release", {"note": "ship it"}, reason="publish 1.2.16")
    answer = now.build()
    blocking = _sections(answer)["blocking"]["items"]
    assert len(blocking) == 1
    assert blocking[0]["kind"] == "approval"
    assert blocking[0]["view"] == "approvals"
    assert blocking[0]["severity"] == now.BLOCKING
    assert blocking[0]["title"] == "publish 1.2.16"
    assert answer["needs_you"] == 1


def test_a_task_that_needs_approval_also_blocks():
    db.create_task(None, "draft the release notes", "write them", provider="mock")
    with db.db() as conn:
        conn.execute("UPDATE tasks SET status = 'needs_approval'")
    answer = now.build()
    blocking = _sections(answer)["blocking"]["items"]
    assert [i["kind"] for i in blocking] == ["task_approval"]
    assert blocking[0]["view"] == "tasks"


def test_the_thing_that_waited_longest_comes_first():
    db.add_approval(1, "older", {})
    db.add_approval(2, "newer", {})
    with db.db() as conn:
        conn.execute("UPDATE agent_logs SET created_at = ? WHERE details LIKE '%older%'",
                     (int(datetime.now().timestamp()) - 3600,))
    answer = now.build()
    blocking = _sections(answer)["blocking"]["items"]
    assert len(blocking) == 2
    assert "older" in blocking[0]["title"]


# --- what is due -------------------------------------------------------------


def test_a_todo_due_today_is_due_today():
    db.add_user_todo("renew the domain", due_date=_day())
    answer = now.build()
    today = _sections(answer)["today"]["items"]
    assert len(today) == 1
    assert today[0]["kind"] == "todo_due"
    assert today[0]["view"] == "todo"
    assert today[0]["severity"] == now.DUE


def test_a_late_todo_blocks_and_says_so():
    db.add_user_todo("send the invoice", due_date=_day(-3))
    answer = now.build()
    today = _sections(answer)["today"]["items"]
    assert len(today) == 1
    assert today[0]["kind"] == "todo_overdue"
    assert today[0]["severity"] == now.BLOCKING
    assert today[0]["meta"]["overdue"] is True


def test_a_todo_due_tomorrow_is_not_today():
    db.add_user_todo("later", due_date=_day(1))
    answer = now.build()
    assert _sections(answer)["today"]["items"] == []
    assert answer["needs_you"] == 0


def test_a_done_todo_is_not_today():
    todo_id = db.add_user_todo("finished", due_date=_day())
    db.update_user_todo(todo_id, status="done")
    answer = now.build()
    assert _sections(answer)["today"]["items"] == []


def test_needs_you_counts_what_cannot_move():
    db.add_approval(1, "a", {})
    db.add_user_todo("b", due_date=_day())
    db.add_user_todo("c", due_date=_day(3))
    answer = now.build()
    assert answer["needs_you"] == 2


def test_a_signal_is_something_to_look_at():
    """Nothing blocks, but something is wrong: the answer still says so, and
    the panel opens on it."""
    db.create_task(None, "publish the site", "do it", provider="mock")
    with db.db() as conn:
        conn.execute("UPDATE tasks SET status = 'error'")
    answer = now.build()
    assert answer["needs_you"] == 0
    assert answer["asks"] == 1
    signals = _sections(answer)["signals"]["items"]
    assert signals[0]["kind"] == "task_error"
    assert signals[0]["view"] == "tasks"


def test_asks_counts_everything_that_wants_a_look():
    db.add_approval(1, "a", {})
    db.add_user_todo("b", due_date=_day())
    db.add_activity("todo_added", "wrote a note")
    answer = now.build()
    # The approval and the todo ask; the activity does not.
    assert answer["needs_you"] == 2
    assert answer["asks"] == 2


# --- what happened -----------------------------------------------------------


def test_what_happened_lands_in_the_context():
    db.add_activity("todo_added", "wrote a note")
    answer = now.build()
    context = _sections(answer)["context"]["items"]
    assert any(i["kind"] == "activity" and i["title"] == "wrote a note" for i in context)


def test_the_context_does_not_ask_for_anything():
    db.add_activity("todo_added", "wrote a note")
    answer = now.build()
    context = _sections(answer)["context"]["items"]
    assert all(i["severity"] == now.CONTEXT for i in context)
    assert answer["needs_you"] == 0


def test_the_morning_briefing_is_part_of_the_context(tmp_path, monkeypatch):
    """The job that runs at 08:00 leaves a note in the vault. The view reads
    that note: the scheduled process is what feeds the panel."""
    from ops_dashboard import db

    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2026-11-11.md").write_text(
        "## 🌅 ConductLoop Briefing — 2026-11-11\n\n- **Open TODOs:** 12\n",
        encoding="utf-8")
    monkeypatch.setattr(db, "vault_root", lambda: vault)
    answer = now.build()
    context = _sections(answer)["context"]["items"]
    notes = [i for i in context if i["kind"] == "daily_note"]
    assert len(notes) == 1
    assert "ConductLoop Briefing" in notes[0]["title"]
    assert "Open TODOs" in notes[0]["detail"]
    assert notes[0]["view"] == "briefing"


# --- the two rules -----------------------------------------------------------


def test_a_broken_source_costs_only_its_own_section(monkeypatch):
    def boom():
        raise RuntimeError("this source is broken")
    monkeypatch.setattr(now, "_approvals", boom)
    db.add_user_todo("still counted", due_date=_day())
    answer = now.build()
    assert answer["ok"] is True
    assert _sections(answer)["blocking"]["items"] == []
    assert len(_sections(answer)["today"]["items"]) == 1


def test_the_answer_never_calls_the_model(monkeypatch):
    """No provider, no key, no network: the answer is still complete."""
    from ops_dashboard import brain

    def boom(*args, **kwargs):
        raise AssertionError("the Now view must not touch the model")

    monkeypatch.setattr(brain, "resolve_provider", boom)
    monkeypatch.setattr(brain, "briefing", boom)
    db.add_user_todo("no model needed", due_date=_day())
    answer = now.build()
    assert answer["needs_you"] == 1


def test_the_answer_writes_nothing(tmp_path, monkeypatch):
    """A glance at the view must not change the workspace."""
    from ops_dashboard import app_paths

    monkeypatch.setattr(app_paths, "path", lambda rel, legacy=None: tmp_path / "nowhere")
    db.add_approval(1, "a", {})
    before = sorted(p.name for p in tmp_path.iterdir())
    now.build()
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    # The pending approval is still pending: nothing was consumed.
    assert len(db.list_pending_approvals()) == 1


def test_the_answer_carries_no_secret():
    db.add_approval(1, "a", {"api_key": "sk-should-never-appear"})
    answer = now.build()
    body = json.dumps(answer)
    assert "sk-should-never-appear" not in body


def test_every_kind_and_section_the_server_can_send_has_words():
    """The panel builds some keys out of what the server sends (``now.kind_``
    + the kind, ``now.section_`` + the section). A kind or a section without
    words would show its key instead of a word, and the guard that checks the
    words of the frame cannot see those keys — it only reads the literals."""
    root = Path(__file__).resolve().parents[1]
    source = (root / "ops_dashboard" / "now.py").read_text(encoding="utf-8")
    kinds = set(re.findall(r'kind="([a-z_]+)"', source))
    sections = set(re.findall(r'\{"id": "([a-z_]+)", "count"', source))
    assert kinds and sections

    web = root / "ops_dashboard" / "web" / "i18n"
    for locale in ("en", "es"):
        dictionary = json.loads((web / f"{locale}.json").read_text(encoding="utf-8"))
        for kind in kinds:
            assert dictionary["now"][f"kind_{kind}"], (locale, kind)
        for section in sections:
            assert dictionary["now"][f"section_{section}"], (locale, section)


# --- over HTTP ---------------------------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "now.db")
    db.init_db()
    import ops_dashboard.server as server_mod

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.DashboardHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port
    httpd.shutdown()


def test_the_api_answers_with_the_whole_thing(server):
    db.add_approval(1, "publish_release", {}, reason="publish 1.2.16")
    db.add_user_todo("renew the domain", due_date=_day())
    with urllib.request.urlopen(f"http://127.0.0.1:{server}/api/now", timeout=10) as r:
        body = json.loads(r.read().decode())
    assert body["ok"] is True
    assert body["needs_you"] == 2
    assert {s["id"] for s in body["sections"]} == {"blocking", "today", "signals", "context"}
    blocking = {s["id"]: s for s in body["sections"]}["blocking"]["items"]
    assert blocking[0]["view"] == "approvals"
