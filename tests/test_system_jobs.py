"""The app's own jobs, and whether the panel can see them.

The built-in jobs — the backup, the briefing, the daily note, the library
index — used to leave no trace: the scheduler caught their failures, wrote
them to the log file and moved on. A monitoring app that cannot see its own
failures is a monitoring app that fails quietly.

These tests check that a run is written down, that the state of a job can be
told from that record, and that the panel can be told about it.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ops_dashboard import db_jobs, scheduler, system_jobs

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
I18N = WEB / "i18n"

# A fixed moment to judge the jobs by, so the tests say the same thing at
# breakfast and at midnight.
NOW = datetime(2026, 11, 10, 10, 30).timestamp()


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    """Every test gets its own database, with the schema of the app."""
    from ops_dashboard import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.db")
    db.init_db()
    # The scheduler's in-memory marks are module-level, like the database:
    # a test that recovers a slot must not stop the next one from
    # recovering the same slot with its own database.
    scheduler._sys_fired.clear()
    scheduler._fired_this_minute.clear()
    return db


# A job that does nothing, so a test can run it without touching the disk.
def _no_job():
    return []


def _db_module():
    """The database module, for the tests that need to age a run."""
    from ops_dashboard import db

    return db


def _fake_job(name: str = "fake_job", spec: str = "0 * * * *", boom: bool = False):
    def fn():
        if boom:
            raise RuntimeError("the disk is full")
        return {"ok": True}
    return [(name, spec, fn)]


# ── the run is written down ─────────────────────────────────────────────────


def test_a_run_is_recorded():
    db_jobs.record_system_job_run("backup", "ok", duration_ms=12)
    last = db_jobs.last_system_job_runs()
    assert "backup" in last
    assert last["backup"]["status"] == "ok"
    assert last["backup"]["duration_ms"] == 12


def test_a_failure_is_recorded_with_its_error():
    db_jobs.record_system_job_run("backup", "error", error="the disk is full")
    last = db_jobs.last_system_job_runs()
    assert last["backup"]["status"] == "error"
    assert last["backup"]["error"] == "the disk is full"


def test_the_last_run_is_the_last_one():
    db_jobs.record_system_job_run("briefing", "error", error="first")
    db_jobs.record_system_job_run("briefing", "ok")
    assert db_jobs.last_system_job_runs()["briefing"]["status"] == "ok"


def test_a_job_that_never_ran_has_no_record():
    assert db_jobs.last_system_job_runs().get("a_job_that_never_ran") is None


def test_the_history_is_kept():
    for _ in range(5):
        db_jobs.record_system_job_run("daily_note", "ok")
    runs = db_jobs.recent_system_job_runs("daily_note")
    assert len(runs) == 5
    assert all(r["job"] == "daily_note" for r in runs)


def test_the_failures_of_the_last_days_are_counted():
    db_jobs.record_system_job_run("briefing", "error", error="once")
    db_jobs.record_system_job_run("briefing", "error", error="twice")
    db_jobs.record_system_job_run("briefing", "ok")
    assert db_jobs.system_job_failure_count("briefing") == 2


# ── the state of a job ──────────────────────────────────────────────────────


def _ran_at(name: str, when: float, status: str = "ok", **kw) -> None:
    """Record a run that happened at a given moment."""
    db_jobs.record_system_job_run(name, status, **kw)
    with _db_module().db() as conn:
        conn.execute("UPDATE system_job_runs SET ran_at = ? WHERE job = ?",
                     (int(when), name))


def _state_of(monkeypatch, name="fake_job", spec="0 * * * *", now=None, **run):
    """The state of a job with one recorded run."""
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job(name, spec))
    if run:
        db_jobs.record_system_job_run(name, run.get("status", "ok"),
                           error=run.get("error"),
                           duration_ms=run.get("duration_ms"))
    return system_jobs.one(name, now=now)


def test_a_job_that_worked_is_ok(monkeypatch):
    job = _state_of(monkeypatch, status="ok")
    assert job["state"] == "ok"


def test_a_job_that_failed_is_in_error(monkeypatch):
    job = _state_of(monkeypatch, status="error", error="the disk is full")
    assert job["state"] == "error"
    assert job["last"]["error"] == "the disk is full"


def test_a_job_that_never_ran_is_never(monkeypatch):
    job = _state_of(monkeypatch)
    assert job["state"] == "never"


def test_a_job_that_stopped_running_is_late(monkeypatch):
    """The failure that used to be invisible: the last run worked, but it
    should have run again by now and has not."""
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job", "0 * * * *"))
    now = datetime(2026, 11, 10, 10, 30).timestamp()
    # A run two hours ago on a job that runs every hour: it missed a slot.
    _ran_at("fake_job", now - 7200)
    assert system_jobs.one("fake_job", now=now)["state"] == "overdue"


def test_a_job_that_ran_recently_is_not_late(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job", "0 * * * *"))
    now = datetime(2026, 11, 10, 10, 30).timestamp()
    _ran_at("fake_job", now)
    assert system_jobs.one("fake_job", now=now)["state"] == "ok"


def test_a_daily_job_that_ran_yesterday_is_not_late_before_its_slot(monkeypatch):
    """Before today's slot, a job that ran yesterday is exactly on time."""
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job", "0 8 * * *"))
    _ran_at("fake_job", datetime(2026, 11, 9, 8, 0).timestamp())
    now = datetime(2026, 11, 10, 7, 0).timestamp()
    assert system_jobs.one("fake_job", now=now)["state"] == "ok"


def test_a_daily_job_that_missed_todays_slot_is_late(monkeypatch):
    """After its slot, a job that ran yesterday is late: today it should
    have run again and did not."""
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job", "0 8 * * *"))
    _ran_at("fake_job", datetime(2026, 11, 9, 8, 0).timestamp())
    now = datetime(2026, 11, 10, 9, 0).timestamp()
    assert system_jobs.one("fake_job", now=now)["state"] == "overdue"


def test_a_job_whose_slot_just_passed_is_not_late_yet(monkeypatch):
    """The grace: the slot passed two minutes ago and the job has not had
    its chance yet. The scheduler ticks every 30 seconds."""
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job", "0 8 * * *"))
    _ran_at("fake_job", datetime(2026, 11, 9, 8, 0).timestamp())
    now = datetime(2026, 11, 10, 8, 2).timestamp()
    assert system_jobs.one("fake_job", now=now)["state"] == "ok"


def test_a_job_whose_slot_passed_long_ago_is_late(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job", "0 8 * * *"))
    _ran_at("fake_job", datetime(2026, 11, 9, 8, 0).timestamp())
    now = datetime(2026, 11, 10, 8, 30).timestamp()
    assert system_jobs.one("fake_job", now=now)["state"] == "overdue"


def test_a_monthly_job_that_ran_on_the_first_is_not_late_on_the_twelfth(monkeypatch):
    """A monthly job is not late because ten days passed. It is late when
    the first of the month went by without a run."""
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job", "0 8 1 * *"))
    _ran_at("fake_job", datetime(2026, 11, 1, 8, 0).timestamp())
    now = datetime(2026, 11, 12, 10, 0).timestamp()
    assert system_jobs.one("fake_job", now=now)["state"] == "ok"


def test_a_monthly_job_that_missed_the_first_is_late(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job", "0 8 1 * *"))
    _ran_at("fake_job", datetime(2026, 10, 1, 8, 0).timestamp())
    now = datetime(2026, 11, 12, 10, 0).timestamp()
    assert system_jobs.one("fake_job", now=now)["state"] == "overdue"


def test_a_job_that_runs_every_five_minutes_is_not_late_after_two_minutes(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job", "*/5 * * * *"))
    now = datetime(2026, 11, 10, 10, 3).timestamp()
    _ran_at("fake_job", now - 120)
    assert system_jobs.one("fake_job", now=now)["state"] == "ok"


def test_a_job_that_runs_every_five_minutes_and_missed_its_slots_is_late(monkeypatch):
    """Every five minutes is every five minutes: a job that ran fifty
    minutes ago missed nine slots, whatever the period looks like."""
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job", "*/5 * * * *"))
    now = datetime(2026, 11, 10, 10, 50).timestamp()
    _ran_at("fake_job", now - 3000)
    assert system_jobs.one("fake_job", now=now)["state"] == "overdue"


def test_the_overview_lists_the_jobs_of_the_scheduler(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: [("backup", "35 * * * *", lambda: None),
                                 ("briefing", "0 8 * * *", lambda: None)])
    over = system_jobs.overview()
    names = [j["name"] for j in over["jobs"]]
    assert names == ["backup", "briefing"]
    assert over["counts"]["never"] == 2


def test_the_jobs_that_need_attention_are_named(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: [("backup", "35 * * * *", lambda: None)])
    db_jobs.record_system_job_run("backup", "error", error="the disk is full")
    over = system_jobs.overview()
    assert "backup" in over["needs_attention"]


# ── the scheduler writes the run down ───────────────────────────────────────


def test_running_a_job_records_it(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job"))
    result = scheduler.run_system_job("fake_job")
    assert result["ok"] is True
    assert db_jobs.last_system_job_runs()["fake_job"]["status"] == "ok"


def test_a_job_that_fails_is_recorded_and_not_swallowed(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job", boom=True))
    result = scheduler.run_system_job("fake_job")
    assert result["ok"] is False
    assert "the disk is full" in result["error"]
    last = db_jobs.last_system_job_runs()["fake_job"]
    assert last["status"] == "error"
    assert "the disk is full" in last["error"]


def test_a_job_that_never_ran_is_not_a_run(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job"))
    assert scheduler.run_system_job("a_job_that_does_not_exist")["ok"] is False


def test_the_scheduled_runs_are_recorded(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job"))
    results = scheduler.run_system_jobs(force=True)
    assert results and results[0]["ok"] is True
    assert db_jobs.last_system_job_runs()["fake_job"]["status"] == "ok"


def test_the_specs_are_the_ones_of_the_scheduler():
    names = [name for name, _spec in scheduler.system_job_specs()]
    for expected in ("backup", "briefing", "daily_note", "library_index"):
        assert expected in names, f"{expected} is not a job of the app"


# ── the panel can see it ────────────────────────────────────────────────────


def _serve():
    from ops_dashboard.server import DashboardHandler

    class Q(DashboardHandler):
        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_the_jobs_are_served():
    srv, base = _serve()
    try:
        with urllib.request.urlopen(base + "/api/system-jobs", timeout=8) as r:
            assert r.status == 200
            data = json.loads(r.read())
        assert data["ok"] is True
        assert data["jobs"], "no jobs at all"
        assert "counts" in data
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_page_of_the_jobs_is_served():
    srv, base = _serve()
    try:
        with urllib.request.urlopen(base + "/jobs", timeout=8) as r:
            assert r.status == 200
            body = r.read().decode("utf-8")
        # The page is strict: no inline code, and it loads its own script.
        assert "/jobs.js" in body, "the page does not load its script"
        assert "<script>" not in body, "the page has inline code"
        policy = r.headers.get("Content-Security-Policy") or ""
        assert "script-src 'self'" in policy, "the page does not use the strict policy"
        assert "unsafe-inline" not in policy
        with urllib.request.urlopen(base + "/jobs.js", timeout=8) as r:
            script = r.read().decode("utf-8")
        assert "/api/system-jobs" in script, "the script does not ask for the jobs"
        with urllib.request.urlopen(base + "/jobs.css", timeout=8) as r:
            assert r.status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_job_can_be_run_from_the_panel(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: _fake_job("fake_job"))
    srv, base = _serve()
    try:
        req = urllib.request.Request(base + "/api/system-jobs/fake_job/run",
                                     data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            assert r.status == 200
            data = json.loads(r.read())
        assert data["ok"] is True
        assert db_jobs.last_system_job_runs()["fake_job"]["status"] == "ok"
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_job_that_does_not_exist_is_not_run():
    srv, base = _serve()
    try:
        req = urllib.request.Request(base + "/api/system-jobs/not_a_job/run",
                                     data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=8)
        except urllib.error.HTTPError as e:
            assert e.code == 404
        else:
            raise AssertionError("an unknown job was run")
    finally:
        srv.shutdown()
        srv.server_close()


# ── the words ───────────────────────────────────────────────────────────────


def test_the_two_languages_say_the_same_about_the_jobs():
    en = json.loads((I18N / "jobs.en.json").read_text(encoding="utf-8"))["jobs"]
    es = json.loads((I18N / "jobs.es.json").read_text(encoding="utf-8"))["jobs"]
    assert set(en) == set(es)


def test_every_job_of_the_app_has_a_name_in_both_languages():
    en = json.loads((I18N / "jobs.en.json").read_text(encoding="utf-8"))["jobs"]
    es = json.loads((I18N / "jobs.es.json").read_text(encoding="utf-8"))["jobs"]
    for name, _spec in scheduler.system_job_specs():
        assert f"job_{name}" in en, f"{name} has no name in English"
        assert f"job_{name}" in es, f"{name} has no name in Spanish"


def test_the_words_the_server_writes_are_in_both_languages():
    en = system_jobs._WORDS["en"]
    es = system_jobs._WORDS["es"]
    assert set(en) == set(es)
    for name, _spec in scheduler.system_job_specs():
        assert name in en, f"{name} has no name in English"
        assert name in es, f"{name} has no name in Spanish"


def test_the_message_of_a_failed_job_is_in_the_language_of_the_person():
    from ops_dashboard import i18n

    i18n.set_locale("es")
    try:
        message = system_jobs.message_of("backup", "error", "el disco está lleno")
        assert "Copia de seguridad" in message
        assert "falló" in message
    finally:
        i18n.set_locale("en")


def test_the_panel_has_a_link_to_the_jobs():
    # The sidebar is written once (app-nav.js) and the panel loads it.
    index = (WEB / "index.html").read_text(encoding="utf-8")
    nav = (WEB / "app-nav.js").read_text(encoding="utf-8")
    assert '<script src="/app-nav.js"></script>' in index
    assert "['link', '/jobs', 'nav.jobs'," in nav, "the panel does not link to the jobs"


def test_the_now_view_says_that_a_job_failed(monkeypatch):
    """A job that failed is something the person has to know about."""
    from ops_dashboard import now as now_mod

    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: [("backup", "35 * * * *", lambda: None)])
    db_jobs.record_system_job_run("backup", "error", error="the disk is full")
    items = now_mod._system_jobs()
    assert items, "the Now view does not mention the failed job"
    assert items[0]["kind"] == "system_job"
    assert items[0]["view"] == "/jobs"
    assert "disk" in items[0]["title"]


def test_the_now_view_says_nothing_when_the_jobs_are_fine(monkeypatch):
    from ops_dashboard import now as now_mod

    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: [("backup", "35 * * * *", lambda: None)])
    db_jobs.record_system_job_run("backup", "ok")
    assert now_mod._system_jobs() == []


# ── the schedule says when a job should have run ────────────────────────────
#
# Guessing the period from the expression ("every hour", "every day") is not
# enough: a monthly job is not late because ten days passed, and a job every
# five minutes is not late because an hour passed. The question is always
# the same one: was there a time it should have run and did not?


def test_the_last_time_a_job_should_have_run_is_found():
    when = datetime(2026, 11, 10, 10, 0)
    assert scheduler.previous_occurrence("0 8 * * *", when) == datetime(2026, 11, 10, 8, 0)


def test_a_job_that_runs_every_five_minutes_has_the_last_five_minutes():
    when = datetime(2026, 11, 10, 10, 3)
    assert scheduler.previous_occurrence("*/5 * * * *", when) == datetime(2026, 11, 10, 10, 0)


def test_a_monthly_job_runs_once_a_month():
    when = datetime(2026, 11, 10, 10, 0)
    assert scheduler.previous_occurrence("0 8 1 * *", when) == datetime(2026, 11, 1, 8, 0)


def test_a_weekly_job_waits_for_its_day():
    # 2026-11-10 is a Tuesday; the job runs on Mondays.
    when = datetime(2026, 11, 10, 10, 0)
    assert scheduler.previous_occurrence("0 4 * * 1", when) == datetime(2026, 11, 9, 4, 0)


def test_a_schedule_that_can_never_fire_says_nothing():
    # The 31st of February: it has never happened and never will.
    when = datetime(2026, 11, 10, 10, 0)
    assert scheduler.previous_occurrence("0 8 31 2 *", when) is None


def test_a_schedule_that_cannot_be_read_says_nothing():
    assert scheduler.previous_occurrence("not a cron", datetime(2026, 11, 10)) is None


def test_the_schedule_of_every_job_of_the_app_can_be_read():
    for name, spec in scheduler.system_job_specs():
        when = scheduler.previous_occurrence(spec, datetime(2026, 11, 10, 10, 0))
        assert when is not None, f"the schedule of {name} ({spec}) cannot be read"


# ── a job that reports its failure in its result ───────────────────────────
#
# Several built-in jobs do not raise when something goes wrong: they report
# it and carry on. Reading only the exception would tell the panel that
# everything worked while every source was failing.


def _job_that_returns(result):
    return [(("fake_job"), "0 8 * * *", lambda: result)]


def test_a_job_that_reports_its_failure_is_a_failure(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _job_that_returns({"ok": False, "error": "the site did not answer"}))
    result = scheduler.run_system_job("fake_job")
    assert result["ok"] is False
    last = db_jobs.last_system_job_runs()["fake_job"]
    assert last["status"] == "error"
    assert "did not answer" in last["error"]


def test_a_job_that_lists_the_sites_that_failed_is_a_failure(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _job_that_returns({"gsc": {"sites": 2, "fetched": 4,
                                                          "errors": ["shop.example"]}}))
    result = scheduler.run_system_job("fake_job")
    assert result["ok"] is False
    assert db_jobs.last_system_job_runs()["fake_job"]["status"] == "error"


def test_a_channel_that_failed_is_a_failure(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _job_that_returns({"daily_note": "/tmp/note.md",
                                                  "discord": "failed: the token expired"}))
    assert scheduler.run_system_job("fake_job")["ok"] is False


def test_a_part_that_was_skipped_because_of_something_is_a_failure(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _job_that_returns({"structure": {"skipped": "the vault stopped answering"}}))
    assert scheduler.run_system_job("fake_job")["ok"] is False


def test_a_job_that_counts_the_files_it_could_not_read_is_not_a_failure(monkeypatch):
    """The index of the vault counts the files it could not parse and still
    did its work: that goes in the summary, not in the state."""
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _job_that_returns({"obsidian": {"indexed": 12, "skipped": 3,
                                                               "errors": 0}}))
    assert scheduler.run_system_job("fake_job")["ok"] is True
    assert db_jobs.last_system_job_runs()["fake_job"]["status"] == "ok"


def test_a_backup_that_was_not_needed_is_not_a_failure(monkeypatch):
    """A backup that already exists today is not a failure: "skipped" here
    is a bool, not a reason."""
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _job_that_returns({"skipped": True, "newest": "ops-2026-11-10.zip"}))
    assert scheduler.run_system_job("fake_job")["ok"] is True


def test_a_channel_that_was_skipped_on_purpose_is_not_a_failure(monkeypatch):
    """The briefing says "skipped" about Telegram on purpose: that is not a
    failure either."""
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _job_that_returns({"telegram": "skipped: it sends only results"}))
    assert scheduler.run_system_job("fake_job")["ok"] is True


# ── a run says what it did ──────────────────────────────────────────────────
#
# "It worked" is half an answer. "It saved 12 rows" is the other half, and
# it is what tells a run that worked from a run that did nothing.


def test_a_run_says_what_it_did(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _job_that_returns({"ga": {"sites": 3, "fetched": 12,
                                                          "upserted": 12},
                                                   "gsc": {"sites": 2, "fetched": 4,
                                                           "errors": []}}))
    scheduler.run_system_job("fake_job")
    job = system_jobs.one("fake_job")
    assert job["did"], "the run did not say what it did"
    assert "rows saved: 12" in job["did"]
    assert "sites: 3" in job["did"]


def test_the_summary_is_in_the_language_of_the_person():
    from ops_dashboard import i18n

    i18n.set_locale("es")
    try:
        text = system_jobs.summary_text(json.dumps({"ga": {"sites": 3, "fetched": 12}}))
        assert "filas bajadas: 12" in text, text
        assert "Analytics" in text
    finally:
        i18n.set_locale("en")


def test_a_list_of_sites_that_failed_is_not_a_count_of_files():
    """The same key, two meanings: the label follows the type of the value."""
    text = system_jobs.summary_text(json.dumps({"gsc": {"errors": ["a.com", "b.com"]}}))
    assert "sites that failed: 2" in text, text


def test_a_run_that_did_nothing_has_nothing_to_say():
    assert system_jobs.summary_text(None) == ""
    assert system_jobs.summary_text("") == ""
    assert system_jobs.summary_text("not json") == ""
    assert system_jobs.summary_text("{}") == ""


def test_the_panel_is_told_what_the_job_did(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _job_that_returns({"ga": {"sites": 3, "fetched": 12,
                                                          "upserted": 12}}))
    scheduler.run_system_job("fake_job")
    srv, base = _serve()
    try:
        with urllib.request.urlopen(base + "/api/system-jobs", timeout=8) as r:
            data = json.loads(r.read())
        job = [j for j in data["jobs"] if j["name"] == "fake_job"][0]
        assert job["did"], "the API does not say what the job did"
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_page_shows_what_the_job_did():
    script = (WEB / "jobs.js").read_text(encoding="utf-8")
    assert "j.did" in script, "the page does not show what the job did"
    css = (WEB / "jobs.css").read_text(encoding="utf-8")
    assert ".job-did" in css, "the summary has no style"


def _the_job(name: str):
    """The function of one of the app's real jobs."""
    for job_name, _spec, fn in scheduler._system_jobs():
        if job_name == name:
            return fn
    raise AssertionError(f"there is no job called {name}")


def test_the_analytics_poll_names_the_sites_that_failed(monkeypatch):
    """The case the bot pointed at: the source catches the error and carries
    on, so the result has to carry it too."""
    from ops_dashboard.indexers import ga as ga_idx
    from ops_dashboard.indexers import gsc as gsc_idx

    monkeypatch.setattr(ga_idx, "run", lambda days=3: {
        "sites": 2, "fetched": 12, "upserted": 12,
        "results": [{"site": "acme.example", "ok": True, "fetched": 12, "upserted": 12},
                    {"site": "shop.example", "ok": False,
                     "error": "the property did not answer"}]})
    monkeypatch.setattr(gsc_idx, "poll_all", lambda days=7: [])
    fn = _the_job("analytics_poll")
    result = fn()
    assert result["ga"]["errors"] == ["shop.example"]
    # And the scheduler writes it down as a failure, not as a healthy run.
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: [("analytics_poll", "30 7 * * *", fn)])
    assert scheduler.run_system_job("analytics_poll")["ok"] is False
    assert db_jobs.last_system_job_runs()["analytics_poll"]["status"] == "error"


def test_the_analytics_poll_of_healthy_sites_is_not_a_failure(monkeypatch):
    from ops_dashboard.indexers import ga as ga_idx
    from ops_dashboard.indexers import gsc as gsc_idx

    monkeypatch.setattr(ga_idx, "run", lambda days=3: {
        "sites": 1, "fetched": 12, "upserted": 12,
        "results": [{"site": "acme.example", "ok": True, "fetched": 12, "upserted": 12}]})
    monkeypatch.setattr(gsc_idx, "poll_all", lambda days=7: [
        {"site": "acme.example", "ok": True, "fetched": 4}])
    fn = _the_job("analytics_poll")
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: [("analytics_poll", "30 7 * * *", fn)])
    assert scheduler.run_system_job("analytics_poll")["ok"] is True


# ── the app catches up on what it missed ────────────────────────────────────
#
# A cron only fires in its own minute: if the Mac was asleep at 08:00, the
# briefing of that day simply never happened. The panel could say "late",
# but the work was still lost. Now the app recovers it.


def test_a_job_that_missed_its_slot_runs_when_the_app_wakes_up(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _fake_job("fake_job", "0 * * * *"))
    # The last run was two hours ago; this job runs every hour.
    _ran_at("fake_job", NOW - 7200)
    out = scheduler.catch_up_system_jobs(datetime.fromtimestamp(NOW))
    assert len(out) == 1, "the job that missed its slot did not run"
    assert out[0]["ok"] is True


def test_a_job_that_ran_in_its_slot_is_not_run_again(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _fake_job("fake_job", "0 * * * *"))
    _ran_at("fake_job", NOW - 60)
    assert scheduler.catch_up_system_jobs(datetime.fromtimestamp(NOW)) == []


def test_a_job_that_never_ran_is_not_caught_up(monkeypatch):
    """A fresh installation has nothing to catch up on, and the person has
    not asked for any of it yet."""
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _fake_job("fake_job", "0 * * * *"))
    assert scheduler.catch_up_system_jobs(datetime.fromtimestamp(NOW)) == []


def test_a_job_that_failed_is_not_caught_up_over_and_over(monkeypatch):
    """The next scheduled run is the retry: a failure is not a reason to try
    again every thirty seconds."""
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _fake_job("fake_job", "0 * * * *", boom=True))
    _ran_at("fake_job", NOW - 7200, status="error", error="the disk is full")
    out = scheduler.catch_up_system_jobs(datetime.fromtimestamp(NOW))
    assert len(out) == 1, "the missed job did not run"
    # The run is written down, so the next tick does not run it again.
    assert scheduler.catch_up_system_jobs(datetime.fromtimestamp(NOW)) == []


def test_only_one_job_is_caught_up_at_a_time(monkeypatch):
    """The scheduler ticks every 30 seconds: the jobs that piled up while the
    app was closed are done one at a time, not all at once."""
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: [
        ("one", "0 * * * *", lambda: {"ok": True}),
        ("two", "0 * * * *", lambda: {"ok": True}),
    ])
    _ran_at("one", NOW - 7200)
    _ran_at("two", NOW - 7200)
    assert len(scheduler.catch_up_system_jobs(datetime.fromtimestamp(NOW))) == 1


def test_a_caught_up_run_says_why_it_ran(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _fake_job("fake_job", "0 * * * *"))
    _ran_at("fake_job", NOW - 7200)
    scheduler.catch_up_system_jobs(datetime.fromtimestamp(NOW))
    last = db_jobs.last_system_job_runs()["fake_job"]
    assert last["kind"] == "catchup"


def test_a_run_on_its_schedule_says_so(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _fake_job("fake_job", "0 * * * *"))
    scheduler.run_system_jobs(force=True)
    last = db_jobs.last_system_job_runs()["fake_job"]
    assert last["kind"] == "scheduled"


def test_a_run_the_person_asked_for_says_so(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _fake_job("fake_job", "0 * * * *"))
    scheduler.run_system_job("fake_job")
    last = db_jobs.last_system_job_runs()["fake_job"]
    assert last["kind"] == "manual"


def test_the_panel_is_told_why_the_job_ran(monkeypatch):
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _fake_job("fake_job", "0 * * * *"))
    _ran_at("fake_job", NOW - 7200)
    scheduler.catch_up_system_jobs(datetime.fromtimestamp(NOW))
    job = system_jobs.one("fake_job", now=NOW)
    assert job["last"]["kind"] == "catchup"


def test_the_page_says_why_the_job_ran():
    script = (WEB / "jobs.js").read_text(encoding="utf-8")
    assert "kindLabel" in script, "the page does not say why the job ran"
    assert "kindLabel(last.kind)" in script, "the page does not use the reason"
    assert "jobs.run_catchup" in script, "the catch-up has no word of its own"
    en = json.loads((I18N / "jobs.en.json").read_text(encoding="utf-8"))["jobs"]
    es = json.loads((I18N / "jobs.es.json").read_text(encoding="utf-8"))["jobs"]
    for key in ("run_scheduled", "run_catchup", "run_manual"):
        assert key in en, f"{key} has no word in English"
        assert key in es, f"{key} has no word in Spanish"


def test_two_schedulers_do_not_both_run_the_missed_job(monkeypatch):
    """The claim comes before the job runs.

    Two schedulers on one database can wake after the same missed slot.
    The one that takes the slot runs the job; the other one does not run it
    at all. Without the claim, both would deliver the morning briefing.
    """
    monkeypatch.setattr(scheduler, "_system_jobs",
                        lambda: _fake_job("fake_job", "0 * * * *"))
    _ran_at("fake_job", NOW - 7200)
    calls = []

    def slow(name, fn, kind="scheduled"):
        # The job is running and has not written anything down yet.
        calls.append(name)
        return {"system_job": name, "ok": True}

    monkeypatch.setattr(scheduler, "_run_and_record", slow)
    assert len(scheduler.catch_up_system_jobs(datetime.fromtimestamp(NOW))) == 1
    # The second scheduler wakes while the first is still running. The
    # in-memory mark is forgotten (it is only a fallback): the claim in the
    # database is what stops it.
    scheduler._sys_fired.clear()
    assert scheduler.catch_up_system_jobs(datetime.fromtimestamp(NOW)) == []
    assert len(calls) == 1, "the job ran twice"


def test_the_briefing_is_recovered_after_the_analytics_it_reads(monkeypatch):
    """The briefing reads the traffic the analytics poll brings in.

    If the analytics is recovered after the briefing, the briefing goes out
    with yesterday's numbers and the anomaly alerts of a day that is over.
    The order of the declaration is not the order of the dependency.
    """
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: [
        ("briefing", "0 8 * * *", lambda: {"ok": True}),
        ("analytics_poll", "30 7 * * *", lambda: {"ok": True}),
    ])
    _ran_at("briefing", NOW - 172800)
    _ran_at("analytics_poll", NOW - 172800)
    out = scheduler.catch_up_system_jobs(datetime.fromtimestamp(NOW), limit=10)
    assert [r["system_job"] for r in out] == ["analytics_poll", "briefing"]


def test_the_daily_note_is_recovered_after_the_library_index(monkeypatch):
    """The daily note covers the notes the library index has already seen."""
    monkeypatch.setattr(scheduler, "_system_jobs", lambda: [
        ("daily_note", "40 8 * * *", lambda: {"ok": True}),
        ("library_index", "17 * * * *", lambda: {"ok": True}),
    ])
    _ran_at("daily_note", NOW - 172800)
    _ran_at("library_index", NOW - 172800)
    out = scheduler.catch_up_system_jobs(datetime.fromtimestamp(NOW), limit=10)
    assert [r["system_job"] for r in out] == ["library_index", "daily_note"]
