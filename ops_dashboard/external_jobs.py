"""Jobs that run outside the app, written down next to the app's own jobs.

The scripts in ``bin/`` and ``scripts/`` are run by launchd (or cron) on a
schedule of their own: the daily Google Analytics and Search Console polls,
the revenue poll, the digest, the URL watch, the boardroom recap. Nothing
wrote down how they went. For weeks every one of them ran the code of an old
release, failed on an import that release still had, and printed
"✓ 7 sites, 0 fetched" — and the panel looked perfectly healthy, because the
panel only knew about the app's own jobs.

A script now says who it is and when it runs, in two variables::

    export OPS_JOB_NAME=ga_poll            # stable key, letters and _
    export OPS_JOB_SCHEDULE="every 86400s" # or a cron expression: "30 8 * * *"

and the CLI commands it calls write their run into the same table as the
app's jobs (``system_job_runs``), judged the same way: a command that raises
failed, and so does one whose result reports a failure without raising (the
poll that caught the error of every site and carried on). The schedule goes
to a small file in the data folder, so the panel can say a script is late
when it stopped running altogether — the failure that used to be invisible.

Without those variables (a person running the CLI by hand, the tests) nothing
is recorded: a run by hand is not the scheduled job.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

ENV_NAME = "OPS_JOB_NAME"
ENV_SCHEDULE = "OPS_JOB_SCHEDULE"

# The jobs of the scripts are named "script.<job>.<command>", so they never
# collide with the app's own jobs and the panel can tell them apart. One script
# can run two commands (the analytics poll runs GA, then Search Console): each
# gets its own line, so a GA failure is not hidden behind a GSC success.
PREFIX = "script."

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_EVERY = re.compile(r"^every\s+(\d{1,7})s$")

# Kept for the panel only: a script that has not run for this long is
# forgotten, so a job that was removed on purpose does not stay red forever.
FORGET_AFTER = 30 * 86400


def registry_path() -> Path:
    from . import app_paths

    return app_paths.data_dir() / "scheduled_scripts.json"


def _load() -> dict:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = registry_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def every_seconds(schedule: str) -> int | None:
    """The interval of an "every <n>s" schedule, or None for a cron one."""
    m = _EVERY.match(schedule.strip())
    return int(m.group(1)) if m else None


def valid_schedule(schedule: str) -> bool:
    if every_seconds(schedule):
        return True
    return len(schedule.split()) == 5


def late(schedule: str, last_run_at: int, now: int) -> bool:
    """Whether a script that should run on ``schedule`` is late.

    A launchd interval starts counting when the job is loaded, not on the
    hour, so "every 86400s" is judged by how long ago it ran (plus a tenth of
    the interval: a Mac that slept runs it on waking). A cron expression is
    judged the way the app judges its own jobs.
    """
    secs = every_seconds(schedule)
    if secs:
        from .scheduler import GRACE

        return now - int(last_run_at) > secs + max(GRACE, secs // 10)
    from .scheduler import slot_missed

    return slot_missed(schedule, last_run_at, now)


def _register(name: str, schedule: str) -> None:
    try:
        data = _load()
        data[name] = {"schedule": schedule, "seen_at": int(time.time())}
        _save(data)
    except OSError:
        log.warning("could not write down the schedule of %s", name, exc_info=True)


def scheduled() -> list[tuple[str, str]]:
    """(name, schedule) of every script seen running in the last 30 days."""
    now = int(time.time())
    out = []
    for name, entry in sorted(_load().items()):
        if not isinstance(entry, dict):
            continue
        schedule = str(entry.get("schedule") or "")
        if not name.startswith(PREFIX) or not valid_schedule(schedule):
            continue
        if now - int(entry.get("seen_at") or 0) > FORGET_AFTER:
            continue
        out.append((name, schedule))
    return out


def job_name(command: str) -> str | None:
    """The name this run is written down as, or None when it is not a
    scheduled script (or the variables say something that is not a key)."""
    job = (os.environ.get(ENV_NAME) or "").strip()
    schedule = (os.environ.get(ENV_SCHEDULE) or "").strip()
    if not job or not schedule:
        return None
    if not _KEY.match(job) or not _KEY.match(command) or not valid_schedule(schedule):
        log.warning("%s=%r / %s=%r: not recorded", ENV_NAME, job, ENV_SCHEDULE, schedule)
        return None
    return f"{PREFIX}{job}.{command}"


def track(command: str, fn):
    """Run ``fn`` and, when a scheduled script called it, write the run down.

    Returns what ``fn`` returns and raises what it raises: the CLI prints the
    same and exits the same; only the record is new.
    """
    name = job_name(command)
    if name is None:
        return fn()
    from . import scheduler
    from .db_jobs import record_system_job_run

    _register(name, os.environ[ENV_SCHEDULE].strip())
    started = time.monotonic()

    def record(status: str, **kw) -> None:
        try:
            record_system_job_run(name, status, kind="external",
                                  duration_ms=int((time.monotonic() - started) * 1000), **kw)
        except Exception:
            log.warning("could not record the run of %s", name, exc_info=True)

    try:
        result = fn()
    except BaseException as e:
        record("error", error=(str(e) or e.__class__.__name__)[:2000])
        raise
    problem = scheduler._problem_in(result)
    summary = scheduler._summary_of(result)
    if problem:
        record("error", error=problem[:2000], summary=summary)
    else:
        record("ok", summary=summary)
    return result
