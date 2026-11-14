"""The runs of the app's own jobs.

The built-in jobs (the backup, the morning briefing, the daily note, the
library index, the vault export, the analytics poll, the learning
maintenance) used to leave no trace anywhere a person could see: the
scheduler caught their failures, wrote them to the log file and moved on.
A monitoring app that cannot see its own failures is a monitoring app that
fails quietly.

One row per run: which job, whether it worked, what the error was, how long
it took and when. The last run of each job is what the panel shows; the
history is what tells a broken job from a job that broke once.
"""
from __future__ import annotations

import time


def _db():
    """The database module.

    Imported here and not at the top: db.py re-exports this module at its
    end, so an import of db from here would close a circle (db -> db_jobs ->
    db) that breaks whoever is imported first.
    """
    from ops_dashboard import db

    return db


# How many runs of each job are kept. The history is there to tell "broken"
# from "broke once"; it does not need to be a log forever.
KEEP_PER_JOB = 30


def record_system_job_run(job: str, status: str, *, error: str | None = None,
                          duration_ms: int | None = None,
                          summary: str | None = None,
                          kind: str = "scheduled") -> int:
    """Write down that a job ran. ``status`` is "ok" or "error".

    ``summary`` is what the job said it did (its result as text): the rows
    it fetched, the files it wrote.

    ``kind`` is why it ran: "scheduled" (its cron said so), "catchup" (it
    missed a slot while the app was closed) or "manual" (the person asked).
    """
    now = int(time.time())
    with _db().db() as conn:
        cur = conn.execute(
            "INSERT INTO system_job_runs (job, status, error, duration_ms, ran_at, summary, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job, status, error, duration_ms, now, summary, kind),
        )
        return int(cur.lastrowid or 0)


def last_system_job_runs() -> dict[str, dict]:
    """The last run of every job that has run, as {job: row}."""
    with _db().db() as conn:
        rows = conn.execute(
            "SELECT r.* FROM system_job_runs r "
            "JOIN (SELECT job, MAX(ran_at) AS ran_at FROM system_job_runs GROUP BY job) t "
            "ON r.job = t.job AND r.ran_at = t.ran_at "
            "ORDER BY r.ran_at DESC, r.id DESC"
        ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        job = row["job"]
        # Two runs in the same second: the newest row wins.
        if job not in out:
            out[job] = dict(row)
    return out


def recent_system_job_runs(job: str | None = None, *, limit: int = 50) -> list[dict]:
    """The runs of one job (or of all of them), newest first."""
    sql = "SELECT * FROM system_job_runs"
    params: list = []
    if job:
        sql += " WHERE job = ?"
        params.append(job)
    sql += " ORDER BY ran_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    with _db().db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def prune_system_job_runs(*, keep_per_job: int = KEEP_PER_JOB) -> int:
    """Forget the runs that are too old to be worth keeping."""
    with _db().db() as conn:
        cur = conn.execute(
            "DELETE FROM system_job_runs WHERE id NOT IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER (PARTITION BY job ORDER BY ran_at DESC, id DESC) AS n"
            "    FROM system_job_runs"
            "  ) WHERE n <= ?"
            ")",
            (int(keep_per_job),),
        )
        return int(cur.rowcount or 0)


def system_job_failure_count(job: str, *, since_days: int = 7) -> int:
    """How many times a job failed in the last few days."""
    since = int(time.time()) - int(since_days) * 86400
    with _db().db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM system_job_runs "
            "WHERE job = ? AND status = 'error' AND ran_at >= ?",
            (job, since),
        ).fetchone()
    return int(row["n"] if row else 0)
