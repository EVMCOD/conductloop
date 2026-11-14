"""The app's own jobs: what they are, how they are doing, and running them now.

GET  /api/system-jobs              the built-in jobs, their cron and their state
POST /api/system-jobs/<name>/run   run one job right now, in front of the person

The jobs are the ones the app runs on its own: the backup, the morning
briefing, the vault export, the analytics poll, the library index, the
daily note and the learning maintenance. They used to be invisible: a job
that failed only reached the log file. Now the panel can say what happened,
and the person can ask for a job to run again without opening a terminal.

Local only: running a job writes to this computer.
"""
from __future__ import annotations

import os
from collections.abc import Callable

from . import scheduler, system_jobs
from .routes_learning import PAGE_CSP, _body, _err, _send_json
from .routing import router

_WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def _local_only(fn: Callable) -> Callable:
    def wrapper(handler, *args, **kwargs) -> None:
        if not handler._is_local():
            _err(handler, 403, "local_only",
                 "the jobs of the app only run on this Mac")
            return
        fn(handler, *args, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _serve(name: str, ctype: str, headers: dict | None = None) -> Callable:
    @_local_only
    def serve(handler) -> None:
        path = os.path.join(_WEB, name)
        if not os.path.exists(path):
            handler._send(404, b"not found", "text/plain")
            return
        with open(path, "rb") as fh:
            body = fh.read()
        handler._send(200, body, ctype, headers or None)
    serve.__name__ = f"serve_{name.replace('.', '_').replace('-', '_')}"
    return serve


@router.get("/jobs")
def jobs_page(handler) -> None:
    """GET /jobs — the page that shows what the app is doing on its own.

    A strict page: no inline code, no inline styles, the policy of the
    learning/chat/sessions pages.
    """
    _serve("jobs.html", "text/html; charset=utf-8",
           {"Content-Security-Policy": PAGE_CSP})(handler)


@router.get("/jobs.css")
def jobs_css(handler) -> None:
    """GET /jobs.css — the styles of the jobs page."""
    _serve("jobs.css", "text/css; charset=utf-8")(handler)


@router.get("/api/system-jobs")
@_local_only
def system_jobs_get(handler) -> None:
    """GET /api/system-jobs — every built-in job and how it is doing."""
    _send_json(handler, 200, system_jobs.overview())


@router.post("/api/system-jobs/<name>/run")
@_local_only
def system_job_run(handler, name: str) -> None:
    """POST /api/system-jobs/<name>/run — run one job now, and say how it went."""
    if _body(handler) is None:
        return
    known = {job for job, _spec in scheduler.system_job_specs()}
    if name not in known:
        _err(handler, 404, "unknown_job", f"{name} is not a job of this app")
        return
    result = scheduler.run_system_job(name)
    payload = {"ok": bool(result.get("ok")), "result": result}
    job = system_jobs.one(name)
    if job:
        payload["job"] = job
    _send_json(handler, 200, payload)
