"""The scheduled scripts (launchd) show up in the Jobs panel next to the app's
own jobs: failed when they fail, late when they stop running.

For weeks the daily GA and Search Console polls ran the code of an old
release, failed on an import in every site and printed "✓ 7 sites, 0
fetched": nothing wrote that down, so the panel looked healthy.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "j.db"))
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    from ops_dashboard import db, i18n

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "j.db")
    db.init_db()
    i18n.set_request_language(None)
    i18n.set_locale("en")
    monkeypatch.delenv("OPS_JOB_NAME", raising=False)
    monkeypatch.delenv("OPS_JOB_SCHEDULE", raising=False)
    return monkeypatch


def _scheduled(monkeypatch, name="analytics_poll", schedule="30 8 * * *"):
    monkeypatch.setenv("OPS_JOB_NAME", name)
    monkeypatch.setenv("OPS_JOB_SCHEDULE", schedule)


def _runs():
    from ops_dashboard.db_jobs import last_system_job_runs

    return last_system_job_runs()


def test_a_run_by_hand_is_not_written_down(env):
    from ops_dashboard import external_jobs

    assert external_jobs.track("ga", lambda: {"sites": 1}) == {"sites": 1}
    assert _runs() == {}


def test_a_scheduled_run_is_written_down_with_its_schedule(env):
    from ops_dashboard import external_jobs

    _scheduled(env)
    assert external_jobs.track("gsc", lambda: [{"site": "Mosaic", "ok": True, "fetched": 3}])[0]["ok"]
    run = _runs()["script.analytics_poll.gsc"]
    assert run["status"] == "ok" and run["kind"] == "external"
    assert ("script.analytics_poll.gsc", "30 8 * * *") in external_jobs.scheduled()


def test_a_site_that_failed_inside_the_result_is_a_failed_run(env):
    """The case that went unseen: no exception, a failure in every site."""
    from ops_dashboard import external_jobs

    _scheduled(env, "ga_poll", "every 86400s")
    result = {"sites": 2, "fetched": 0, "upserted": 0, "results": [
        {"site": "Mosaic", "ok": False, "error": "No module named 'google.analytics'"},
        {"site": "Tidepool", "ok": False, "error": "No module named 'google.analytics'"},
    ]}
    assert external_jobs.track("ga", lambda: result) is result
    run = _runs()["script.ga_poll.ga"]
    assert run["status"] == "error"
    assert "google.analytics" in run["error"]


def test_a_command_that_raises_is_written_down_and_still_raises(env):
    from ops_dashboard import external_jobs

    _scheduled(env, "revenue_poll", "every 3600s")

    def boom():
        raise RuntimeError("the database is locked")

    with pytest.raises(RuntimeError):
        external_jobs.track("revenue", boom)
    assert _runs()["script.revenue_poll.revenue"]["error"] == "the database is locked"


def test_variables_that_are_not_keys_are_not_recorded(env):
    from ops_dashboard import external_jobs

    _scheduled(env, "Ga Poll; rm -rf", "every 60s")
    external_jobs.track("ga", lambda: {})
    _scheduled(env, "ga_poll", "whenever")
    external_jobs.track("ga", lambda: {})
    assert _runs() == {}


def test_the_panel_lists_the_scripts_and_says_when_one_is_late(env):
    from ops_dashboard import external_jobs, system_jobs

    _scheduled(env, "ga_poll", "every 86400s")
    external_jobs.track("ga", lambda: {"sites": 1, "results": [{"site": "Mosaic", "ok": True}]})
    ran_at = _runs()["script.ga_poll.ga"]["ran_at"]

    job = system_jobs.one("script.ga_poll.ga", now=ran_at + 3600)
    assert job["state"] == "ok" and job["external"] is True
    assert job["label"] == "Daily Analytics poll (script) · Google Analytics"

    # A day and a tenth later with no run: launchd stopped running it.
    late = system_jobs.one("script.ga_poll.ga", now=ran_at + 86400 + 8640 + 60)
    assert late["state"] == "overdue"
    assert "script.ga_poll.ga" in system_jobs.overview(now=ran_at + 96000)["needs_attention"]

    # The app's own jobs are still there.
    names = [j["name"] for j in system_jobs.overview()["jobs"]]
    assert "backup" in names


def test_a_script_on_a_cron_is_late_after_its_slot(env):
    from ops_dashboard import external_jobs

    assert external_jobs.late("0 8 * * *", last_run_at=_ts(2026, 11, 14, 8, 0), now=_ts(2026, 11, 15, 9, 0))
    assert not external_jobs.late("0 8 * * *", last_run_at=_ts(2026, 11, 15, 8, 0), now=_ts(2026, 11, 15, 9, 0))
    assert not external_jobs.late("every 300s", last_run_at=1000, now=1000 + 300 + 600)
    assert external_jobs.late("every 300s", last_run_at=1000, now=1000 + 300 + 601)


def _ts(*parts) -> int:
    import datetime as dt

    return int(dt.datetime(*parts).timestamp())


def test_a_script_not_seen_for_a_month_is_forgotten(env):
    import json

    from ops_dashboard import external_jobs

    external_jobs.registry_path().write_text(json.dumps({
        "script.old.ga": {"schedule": "every 60s", "seen_at": int(time.time()) - 31 * 86400},
        "script.new.ga": {"schedule": "every 60s", "seen_at": int(time.time())},
        "not_a_script": {"schedule": "every 60s", "seen_at": int(time.time())},
    }))
    assert [n for n, _ in external_jobs.scheduled()] == ["script.new.ga"]


def test_the_names_read_in_spanish(env):
    from ops_dashboard import i18n, system_jobs

    i18n.set_locale("es")
    assert system_jobs.name_of("script.analytics_poll.gsc") == \
        "Consulta de analítica de la mañana (script) · Search Console"
    assert system_jobs.name_of("script.digest.digest") == "Digest diario (script)"


def test_the_cli_poll_writes_its_run_down(env):
    from typer.testing import CliRunner

    from ops_dashboard import cli
    from ops_dashboard.indexers import ga as ga_idx

    _scheduled(env, "ga_poll", "every 86400s")
    env.setattr(ga_idx, "run", lambda **kw: {"sites": 1, "fetched": 0, "upserted": 0, "results": [
        {"site": "Mosaic", "ok": False, "error": "403 Forbidden"}]})
    out = CliRunner().invoke(cli.app, ["ga", "poll"])
    assert out.exit_code == 0, out.output
    assert _runs()["script.ga_poll.ga"]["error"] == "403 Forbidden"


SCRIPTS = {
    "bin/poll_ga.sh": "ga_poll",
    "bin/poll_revenue.sh": "revenue_poll",
    "bin/run_digest.sh": "digest",
    "bin/run_recap_cron.sh": "recap",
    "bin/check_urls.sh": "url_watch",
    "scripts/poll_analytics.sh": "analytics_poll",
}


@pytest.mark.parametrize("path,name", sorted(SCRIPTS.items()))
def test_every_scheduled_script_says_who_it_is_and_when_it_runs(path, name):
    from ops_dashboard import external_jobs

    text = (ROOT / path).read_text()
    assert f"export OPS_JOB_NAME={name}\n" in text
    m = re.search(r'export OPS_JOB_SCHEDULE="\$\{OPS_JOB_SCHEDULE:-([^}]+)\}"', text)
    assert m and external_jobs.valid_schedule(m.group(1))
    # After the code check, so a job that runs the wrong code still says so.
    assert text.index("ops_check_code\n") < text.index("export OPS_JOB_NAME")


def test_the_panel_has_no_run_button_for_a_script():
    js = (ROOT / "ops_dashboard/web/jobs.js").read_text()
    assert "j.external ? '' : '<button data-run=" in js
    assert "external: 'jobs.run_external'" in js
