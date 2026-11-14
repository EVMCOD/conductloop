"""The reaper: what happens to video jobs nobody is watching.

Video is asynchronous and costs money. Without polling, a finished render is
never downloaded. With unlimited polling, a job whose task the engine has
forgotten is retried for ever on every scheduler tick. These tests pin both
ends of that.
"""
from __future__ import annotations

import time

import pytest

from ops_dashboard import db_media, media


class _Engine(media.MediaEngine):
    """Records polls; settles however the test tells it to."""

    name = "engine"

    def __init__(self, behaviour="succeed"):
        self.behaviour = behaviour
        self.polls: list[str] = []

    def available(self):
        return True

    def submit_video(self, prompt, **kw):
        return media.MediaResult(ok=True, kind="video", task_id="t-reap", status="queued",
                                 meta={"model": kw.get("model")})

    def poll_video(self, task_id, **kw):
        self.polls.append(task_id)
        if self.behaviour == "raise":
            raise media.MediaError("the engine forgot this task")
        if self.behaviour == "pending":
            return media.MediaResult(ok=True, kind="video", task_id=task_id, status="queued")
        return media.MediaResult(ok=True, kind="video", task_id=task_id, status="succeeded",
                                 path=_mp4(), url="https://engine.invalid/v.mp4")


def _mp4():
    import os
    from pathlib import Path
    p = Path(os.environ.get("OPS_DATA_DIR", "/tmp")) / "reap.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00\x00\x00 ftypmp42stub")
    return str(p)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(media.app_paths, "data_dir", lambda: tmp_path)
    db.init_db()
    _wipe()
    yield _Engine()
    _wipe()
    media.set_engine(None)


def _wipe() -> None:
    """The suite shares one database; leftover jobs would be reaped here."""
    with db_media._dbmod().db() as conn:
        conn.execute("DELETE FROM media_jobs")
        conn.execute("DELETE FROM media_assets")


def _queue(prompt="a shot", **meta) -> int:
    return db_media.add_job(kind="video", prompt=prompt, engine="engine",
                            model="MiniMax-H3", external_task_id="t-reap",
                            status="queued", meta=meta)


# ── the happy path still works ─────────────────────────────────────────────

def test_the_reaper_downloads_a_finished_render(engine):
    media.set_engine(engine)
    job_id = _queue()
    out = media.reap_jobs()
    assert out["succeeded"] == 1
    job = db_media.get_job(job_id)
    assert job["status"] == "succeeded"
    assert job["asset_id"]


def test_a_job_still_rendering_is_left_alone(engine):
    engine.behaviour = "pending"
    media.set_engine(engine)
    job_id = _queue()
    out = media.reap_jobs()
    assert out["succeeded"] == 0
    assert db_media.get_job(job_id)["status"] in ("queued", "running")


# ── a job that cannot finish must not last for ever ────────────────────────

def test_a_stuck_job_is_eventually_given_up_on(engine):
    """Polling an expired task for ever would re-ask on every scheduler tick."""
    engine.behaviour = "pending"
    media.set_engine(engine)
    job_id = _queue(attempts=media.MAX_JOB_ATTEMPTS - 1)
    out = media.reap_jobs()
    assert out["given_up"] == 1
    job = db_media.get_job(job_id)
    assert job["status"] == "failed"
    assert "did not complete" in (job["error"] or "")
    # And it says why, rather than just stopping.
    assert engine.polls == []


def test_a_job_older_than_the_limit_is_given_up_on_without_more_polling(engine):
    engine.behaviour = "pending"
    media.set_engine(engine)
    job_id = _queue()
    with db_media._dbmod().db() as conn:
        conn.execute("UPDATE media_jobs SET created_at = ? WHERE id = ?",
                     (int(time.time()) - media.MAX_JOB_AGE_SECONDS - 60, job_id))
    out = media.reap_jobs()
    assert out["given_up"] == 1
    assert db_media.get_job(job_id)["status"] == "failed"


def test_a_failing_poll_is_recorded_and_retried_before_giving_up(engine):
    engine.behaviour = "raise"
    media.set_engine(engine)
    job_id = _queue()
    out = media.reap_jobs()
    # Still pending: one hiccup is not a dead render.
    assert out["given_up"] == 0
    assert out["checked"] == 1
    job = db_media.get_job(job_id)
    assert job["status"] != "failed"
    assert "forgot this task" in job["meta"]["last_error"]
    assert job["meta"]["attempts"] == 1


def test_the_attempt_counter_reaches_the_limit_and_stops_the_polling(engine):
    engine.behaviour = "raise"
    media.set_engine(engine)
    job_id = _queue()
    for _ in range(media.MAX_JOB_ATTEMPTS):
        media.reap_jobs()
    assert db_media.get_job(job_id)["status"] == "failed"
    polls = len(engine.polls)
    media.reap_jobs()
    assert len(engine.polls) == polls, "a failed job must not be polled again"


def test_one_broken_job_does_not_stop_the_others(engine):
    """A job that raises must not take the rest of the queue down with it."""
    engine.behaviour = "raise"
    media.set_engine(engine)
    broken = _queue("broken")
    engine.behaviour = "succeed"
    healthy = _queue("healthy")
    # Make the first one the broken one by id order.
    assert broken < healthy
    out = media.reap_jobs()
    assert out["checked"] == 2
    assert db_media.get_job(healthy)["status"] == "succeeded"
    assert db_media.get_job(broken)["status"] != "failed"


def test_finished_jobs_are_not_polled_again(engine):
    media.set_engine(engine)
    _queue()
    media.reap_jobs()
    polls = len(engine.polls)
    media.reap_jobs()
    assert len(engine.polls) == polls
