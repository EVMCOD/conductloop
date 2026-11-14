"""Agent scheduler: runs in background, fires triggers, wakes agents.

Two trigger types are supported:
  - cron: standard 5-field cron expressions. Matched against current minute.
  - webhook: HTTP-triggered (handled inline in the route, not here).
  - event: future (e.g. url_down, code_todo_done) — TBD.

The scheduler is started by `serve --with-scheduler` and ticks every 30s.
It iterates all enabled triggers, checks if their cron matches the current
minute, and if so wakes the agent with the trigger's prompt.

Each wake:
  1. Logs an `agent_logs` row (action='wake')
  2. Calls brain with the agent's memory + reporting chain as system prompt
  3. Records the response and any artifacts the agent creates
  4. Marks the trigger as fired (last_fired_at, fire_count++)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

from . import brain, db, provider_errors

log = logging.getLogger("scheduler")

# "Did this cron already fire this minute?" The scheduler ticks every 30s, so
# a matching minute is seen twice. The mark that counts is the row in
# ``scheduler_fires`` (see ``_claim_minute``): it survives a restart inside
# the same minute, and two processes on one database share it. These dicts
# are only the fallback for when that row cannot be written.
_fired_this_minute: dict[int, str] = {}  # trigger_id -> minute string
_sys_fired: dict[str, str] = {}          # system-job name -> minute string

_FIRES_DDL = (
    "CREATE TABLE IF NOT EXISTS scheduler_fires ("
    " job TEXT PRIMARY KEY,"          # 'trigger:<id>' or 'system:<name>'
    " minute TEXT NOT NULL,"          # local 'YYYY-MM-DD HH:MM' it last fired in
    " fired_at INTEGER NOT NULL)"
)


def _claim_minute(job: str, minute_key: str) -> bool:
    """Take ``minute_key`` for ``job``, once, in the database.

    True for the first caller in that minute: this process, a restarted one,
    or another process using the same database. False for every later one.
    Raises if the database cannot be written; ``_should_fire`` falls back to
    the in-memory mark then, so a broken disk does not stop the scheduler.
    """
    conn = db.get_connection()
    try:
        conn.execute(_FIRES_DDL)
        cur = conn.execute(
            "INSERT INTO scheduler_fires (job, minute, fired_at) VALUES (?, ?, ?) "
            "ON CONFLICT(job) DO UPDATE SET minute = excluded.minute, "
            "fired_at = excluded.fired_at WHERE scheduler_fires.minute <> excluded.minute",
            (job, minute_key, int(time.time())),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _should_fire(job: str, memo: dict, key, minute_key: str) -> bool:
    """Whether a cron that matches this minute still has to run in it."""
    if memo.get(key) == minute_key:
        return False
    try:
        first = _claim_minute(job, minute_key)
    except Exception:
        log.warning("could not record that %s fired; deduping in memory only", job,
                    exc_info=True)
        first = True
    memo[key] = minute_key
    return first


def _system_jobs() -> list[tuple[str, str, object]]:
    """Built-in scheduled jobs. Cron specs overridable via env. Each fn is a
    zero-arg callable run when its cron matches — this is what makes the morning
    briefing / vault export actually fire on their own."""
    from . import briefing, obsidian_sync

    def _briefing():
        chans = [c.strip() for c in os.environ.get("BRIEFING_CHANNELS", "").split(",") if c.strip()]
        return briefing.deliver(channels=chans)

    def _export():
        return obsidian_sync.export_all()

    def _analytics():
        # Refresh GA4 + Search Console daily metrics. Small windows: both
        # sources lag 1-3 days, so a few days of overlap re-upserts cleanly
        # (idempotent on (site_id, date)). Runs before the 08:00 briefing so
        # the briefing sees fresh traffic numbers.
        from .indexers import ga as ga_idx
        from .indexers import gsc as gsc_idx
        ga_res = ga_idx.run(days=3)
        gsc_res = gsc_idx.poll_all(days=7)
        # The sites that failed are named. A source that failed is not a
        # source that worked quietly: the panel has to be able to say that
        # the traffic numbers of one site are missing.
        ga_errors = [r["site"] for r in ga_res.get("results", [])
                     if not r.get("ok")]
        gsc_errors = [r["site"] for r in gsc_res if not r.get("ok")]
        return {
            "ga": {**{k: ga_res.get(k) for k in ("sites", "fetched", "upserted")},
                   "errors": ga_errors},
            "gsc": {"sites": len(gsc_res),
                    "fetched": sum(r.get("fetched", 0) for r in gsc_res),
                    "errors": gsc_errors},
        }

    def _library_index():
        # Incremental (unchanged files are skipped): without it the library
        # stayed at the last manual index, four months stale on one install.
        from . import library
        return library.refresh()

    def _learning():
        # L8: failure-log retention (D6) + skills curator that only PROPOSES
        # archiving skills unused for 60 days (D14). Never moves or deletes.
        from .learning import metrics
        return metrics.scheduled_maintenance()

    def _daily_note():
        # The daily note of the vault, with what happened in it. It runs
        # after the library index (which is hourly, at :17) so the notes
        # touched that day are already in the index.
        from . import daily
        out = daily.write_daily()
        return {"note": str(out)}

    def _backup():
        # Hourly check, at most one backup a day: a fixed time of day would be
        # missed every day the Mac sleeps through it.
        from . import backups
        return backups.scheduled()

    return [
        ("backup", os.environ.get("BACKUP_CRON", "35 * * * *"), _backup),
        ("learning_maintenance", os.environ.get("LEARNING_MAINTENANCE_CRON", "15 4 * * *"), _learning),
        ("briefing", os.environ.get("BRIEFING_CRON", "0 8 * * *"), _briefing),
        ("obsidian_export", os.environ.get("OBSIDIAN_EXPORT_CRON", "0 3 * * *"), _export),
        ("analytics_poll", os.environ.get("ANALYTICS_POLL_CRON", "30 7 * * *"), _analytics),
        ("library_index", os.environ.get("LIBRARY_INDEX_CRON", "17 * * * *"), _library_index),
        ("daily_note", os.environ.get("DAILY_NOTE_CRON", "40 8 * * *"), _daily_note),
    ]


def _allowed(field: str, bounds: tuple[int, int], dow_field: bool = False) -> list[int]:
    """Every value of a cron field that matches it, in order."""
    lo, hi = bounds
    return [v for v in range(lo, hi + 1)
            if _match_field(field, v, dow_field=dow_field, bounds=bounds)]


def previous_occurrence(expr: str, now: datetime) -> datetime | None:
    """The last time a cron expression would have fired, at or before `now`.

    This is what tells a job that missed its slot from a job that is simply
    not due yet. Guessing the period from the expression ("every hour", "every
    day") is not enough: a monthly job (``0 8 1 * *``) is not late because ten
    days passed, and a job every five minutes (``*/5 * * * *``) is not late
    because an hour passed.

    None when the expression never fires (or cannot be understood): then
    nothing can be said about it being late.
    """
    try:
        fields = expr.split()
        if len(fields) != 5:
            return None
        minute_f, hour_f, dom_f, month_f, dow_f = fields
        minutes = _allowed(minute_f, _CRON_BOUNDS[0])
        hours = _allowed(hour_f, _CRON_BOUNDS[1])
        if not minutes or not hours:
            return None
        dom_restricted = dom_f.strip() != "*"
        dow_restricted = dow_f.strip() != "*"
        day = now.date()
        # A year is more than enough to find the previous occurrence of any
        # expression that fires at all (the longest gap is a yearly one).
        for _ in range(400):
            dom_ok = _match_field(dom_f, day.day, bounds=_CRON_BOUNDS[2])
            month_ok = _match_field(month_f, day.month, bounds=_CRON_BOUNDS[3])
            dow_ok = _match_field(dow_f, (day.weekday() + 1) % 7,
                                  dow_field=True, bounds=_CRON_BOUNDS[4])
            # Standard cron: when BOTH day-of-month and day-of-week are
            # restricted, the job fires if EITHER of them matches.
            if dom_restricted and dow_restricted:
                day_ok = dom_ok or dow_ok
            else:
                day_ok = dom_ok and dow_ok
            if day_ok and month_ok:
                for hour in reversed(hours):
                    for minute in reversed(minutes):
                        cand = datetime(day.year, day.month, day.day, hour, minute)
                        if cand <= now:
                            return cand
            day -= timedelta(days=1)
        return None
    except Exception:
        return None


def _problem_in(result, _depth: int = 0) -> str | None:
    """A failure the job reported in its result instead of raising.

    Several built-in jobs do not raise when something goes wrong: they
    report it and carry on. ``GA4Source.run()`` and ``GSCSource.run()``
    catch the polling error and return ``{"ok": False, "error": ...}``; the
    briefing writes ``"failed: ..."`` per channel; the library index says
    ``{"skipped": "the vault stopped answering"}``. Reading only the
    exception would say those jobs worked.

    Returns what went wrong, or None when the result says nothing wrong.
    """
    if _depth > 4:
        return None
    if isinstance(result, dict):
        if result.get("ok") is False:
            error = result.get("error")
            return str(error) if error else "the job reported a failure"
        errors = result.get("errors")
        if isinstance(errors, (list, tuple)) and errors:
            # A list names what failed ("acme.example", "shop.example"). A plain
            # count is a counter, not a report: the index of the vault counts
            # the files it could not parse and still did its work.
            return ", ".join(str(e) for e in errors) or "the job reported errors"
        if isinstance(errors, str) and errors.strip():
            return errors
        error = result.get("error")
        if isinstance(error, str) and error.strip():
            return error
        skipped = result.get("skipped")
        # {"skipped": True} means "nothing to do"; a string means "skipped
        # because of this".
        if isinstance(skipped, str) and skipped.strip():
            return skipped
        for value in result.values():
            found = _problem_in(value, _depth + 1)
            if found:
                return found
        return None
    if isinstance(result, (list, tuple)):
        for item in result:
            found = _problem_in(item, _depth + 1)
            if found:
                return found
        return None
    if isinstance(result, str) and result.strip().lower().startswith("failed"):
        return result
    return None


def _run_and_record(name: str, fn, kind: str = "scheduled") -> dict:
    """Run one built-in job and write down what happened.

    A job that fails is not lost: the failure is recorded, so the panel can
    show it, and the run is not retried behind the person's back (the next
    scheduled run is the retry).

    A job that reports the failure in its result instead of raising counts
    as a failure too: the analytics poll that caught the error of every
    source and carried on has not worked.

    ``kind`` says why it ran: on its schedule, because it missed one while
    the app was closed, or because the person asked for it. Without that,
    a backup at 14:00 is a mystery.
    """
    started = time.monotonic()
    try:
        result = fn()
    except Exception as e:
        log.exception("system job %s failed", name)
        duration = int((time.monotonic() - started) * 1000)
        error = str(e) or e.__class__.__name__
        _record_run(name, "error", error=error, duration_ms=duration, kind=kind)
        return {"system_job": name, "ok": False, "error": error}
    duration = int((time.monotonic() - started) * 1000)
    summary = _summary_of(result)
    problem = _problem_in(result)
    if problem:
        _record_run(name, "error", error=problem, duration_ms=duration,
                    summary=summary, kind=kind)
        return {"system_job": name, "ok": False, "error": problem,
                "duration_ms": duration}
    _record_run(name, "ok", duration_ms=duration, summary=summary, kind=kind)
    return {"system_job": name, "ok": True, "duration_ms": duration}


def _summary_of(result) -> str | None:
    """What the job says it did, as text small enough to keep."""
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        return None
    if not text or text in ("null", "{}", "[]"):
        return None
    return text[:2000]


# How long a job has to run its slot before it counts as late, and before it
# is worth catching up. The scheduler ticks every 30 seconds, so this is
# generous: it is there for the tick that arrives late, not for a job that
# stopped.
GRACE = 600


def slot_missed(spec: str, last_run_at: int, now: int) -> bool:
    """Whether the job missed a slot it should have run in.

    The question is not "how long ago did it run" but "was there a time it
    should have run and did not". The schedule says when that was: a job
    every five minutes is not late because an hour passed, and a monthly
    job is not late because ten days passed.

    The grace period is for the scheduler ticking half a minute late, not
    for a job that stopped.
    """
    slot = last_slot(spec, now)
    if slot is None:
        # The schedule says nothing (or cannot be understood): nothing can be
        # said about it being late.
        return False
    return int(last_run_at) < int(slot.timestamp())


def last_slot(spec: str, now: int) -> datetime | None:
    """The slot the job should have served by now, if any.

    This is the occurrence the cron had *before* the grace: the job that
    owes work is the one whose turn came and went more than half a minute
    ago.
    """
    when = datetime.fromtimestamp(max(0, int(now) - GRACE))
    return previous_occurrence(spec, when)


# Jobs other jobs depend on, recovered first. The briefing reads the traffic
# the analytics poll brings in, and the daily note covers the notes the
# library index has already seen. Recovering them the other way round would
# deliver a briefing with yesterday's numbers and a daily note about notes
# nobody indexed.
RECOVERED_FIRST = ("analytics_poll", "library_index")


def catch_up_system_jobs(now: datetime | None = None, *, limit: int = 1) -> list[dict]:
    """Run the jobs that missed their slot while the app was not looking.

    A cron only fires in its own minute: if the Mac was asleep at 08:00,
    the briefing of that day simply never happened, and nothing in the app
    ever recovered it. The panel could say "late", but the work was still
    lost.

    This is the recovery: a job that missed its slot runs now, once. The
    run is written down like any other, so the panel can say what it did.

    **One job per call.** The scheduler ticks every 30 seconds, so the jobs
    that piled up while the app was closed are done one at a time instead
    of all at once when it wakes up.

    **The turn is claimed before the job runs.** Two schedulers on one
    database that wake after the same missed slot must not both deliver
    the briefing: the claim is the same one the scheduled runs use, taken
    before the callback, not after it.

    A job that has never run is not caught up: a fresh installation has
    nothing to catch up on, and the person has not asked for any of it yet.
    """
    now = now or datetime.now()
    try:
        from .db_jobs import last_system_job_runs

        lasts = last_system_job_runs()
    except Exception:
        log.warning("could not read the last runs of the system jobs",
                    exc_info=True)
        return []
    out: list[dict] = []
    stamp = int(now.timestamp())
    for name, spec, fn in _recovery_order():
        if len(out) >= limit:
            break
        last = lasts.get(name)
        if not last:
            continue
        slot = last_slot(spec, stamp)
        if slot is None:
            continue
        if int(last.get("ran_at") or 0) >= int(slot.timestamp()):
            continue
        # The claim comes first: whoever takes the slot runs the job, the
        # other one does not run it at all.
        slot_key = f"catchup:{int(slot.timestamp())}"
        if not _should_fire(f"system:{name}", _sys_fired, f"catchup:{name}",
                             slot_key):
            continue
        log.info("catching up the system job %s: it missed its slot", name)
        out.append(_run_and_record(name, fn, kind="catchup"))
    return out


def _recovery_order() -> list[tuple[str, str, object]]:
    """The jobs in the order their missed slots should be recovered."""
    jobs = _system_jobs()
    first = [j for j in jobs if j[0] in RECOVERED_FIRST]
    rest = [j for j in jobs if j[0] not in RECOVERED_FIRST]
    return first + rest


def _record_run(name: str, status: str, *, error: str | None = None,
                duration_ms: int | None = None,
                summary: str | None = None,
                kind: str = "scheduled") -> None:
    """Write the run down. A failure to write must not break the job."""
    try:
        from .db_jobs import record_system_job_run

        record_system_job_run(name, status, error=error, duration_ms=duration_ms,
                              summary=summary, kind=kind)
    except Exception:
        log.warning("could not record the run of the system job %s", name, exc_info=True)


def system_job_specs() -> list[tuple[str, str]]:
    """The built-in jobs and the cron each one runs on.

    The cron can be overridden with the environment, so the list is built
    when it is asked for, not once at import.
    """
    return [(name, spec) for name, spec, _fn in _system_jobs()]


def run_system_job(name: str) -> dict:
    """Run one built-in job right now, by name, and record the run.

    This is what the "run now" button in the panel calls: the person asks
    for the backup to happen and it happens, in front of them. The run is
    marked as theirs, so the panel does not present it as the schedule's.
    """
    for job_name, _spec, fn in _system_jobs():
        if job_name == name:
            return _run_and_record(job_name, fn, kind="manual")
    return {"system_job": name, "ok": False, "error": "unknown job"}


def run_system_jobs(now: datetime | None = None, *, force: bool = False) -> list[dict]:
    """Run any system job whose cron matches `now` (or all, if force). Deduped
    per minute like agent triggers."""
    now = now or datetime.now()
    minute_key = now.strftime("%Y-%m-%d %H:%M")
    out = []
    for name, spec, fn in _system_jobs():
        if not force:
            if _sys_fired.get(name) == minute_key:
                continue
            if not _cron_matches(spec, now):
                continue
            if not _should_fire(f"system:{name}", _sys_fired, name, minute_key):
                continue
        out.append(_run_and_record(name, fn))
    return out


# Standard cron day-of-week numbering: 0 and 7 are Sunday, 1 is Monday.
# The old implementation used Python's weekday() (Monday=0), so every cron
# expression written anywhere else meant a different day, and name ranges
# like "MON-FRI" never matched at all. Fixed 2026-10-23.
_CRON_DOW_NAMES = {
    "SUN": 0, "MON": 1, "TUE": 2, "WED": 3,
    "THU": 4, "FRI": 5, "SAT": 6,
}

# Bounds per field: minute, hour, day-of-month, month, day-of-week.
_CRON_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def _cron_dow_value(now: datetime) -> int:
    """Day of week in standard cron numbering (Sunday=0, Monday=1)."""
    return (now.weekday() + 1) % 7


def _cron_value(token: str, dow_field: bool) -> int | None:
    """Parse one cron field token (a number, or a day name) into a value."""
    token = token.strip()
    if not token:
        return None
    if dow_field:
        named = _CRON_DOW_NAMES.get(token.upper())
        if named is not None:
            return named
    try:
        n = int(token)
    except ValueError:
        return None
    if dow_field and n == 7:
        return 0  # standard cron: 7 is Sunday too
    return n


def _cron_matches(expr: str, now: datetime) -> bool:
    """Match a 5-field cron expression against `now`. Supports *, */N, N, N-M, N,M."""
    try:
        fields = expr.split()
        if len(fields) != 5:
            return False
        minute, hour, dom, month, dow = fields
        vals = [now.minute, now.hour, now.day, now.month, _cron_dow_value(now)]
        minute_ok, hour_ok, dom_ok, month_ok, dow_ok = (
            _match_field(f, v, dow_field=(i == 4), bounds=_CRON_BOUNDS[i])
            for i, (f, v) in enumerate(zip(fields, vals))
        )
        # Standard cron: when BOTH day-of-month and day-of-week are restricted
        # (not "*"), the job fires if EITHER of them matches.
        if dom.strip() != "*" and dow.strip() != "*":
            day_ok = dom_ok or dow_ok
        else:
            day_ok = dom_ok and dow_ok
        return bool(minute_ok and hour_ok and month_ok and day_ok)
    except Exception:
        return False


def _match_field(field: str, value: int, dow_field: bool = False,
                 bounds: tuple[int, int] = (0, 59)) -> bool:
    """Match a single cron field. value: minute 0-59, hour 0-23, dom 1-31, month 1-12, dow 0-6 (Sun=0)."""
    lo, hi = bounds
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        base = part
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                continue
            if step <= 0:
                continue
        base = base.strip()
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            lo_s, hi_s = base.split("-", 1)
            start = _cron_value(lo_s, dow_field)
            end = _cron_value(hi_s, dow_field)
            if start is None or end is None:
                continue
        else:
            v = _cron_value(base, dow_field)
            if v is None:
                continue
            # "5/2" means "from 5, every 2, up to the field maximum".
            start, end = (v, hi) if step > 1 else (v, v)
        if start is None or end is None:
            continue
        if start < lo or end > hi or start > end:
            continue
        if not (start <= value <= end):
            continue
        if (value - start) % step == 0:
            return True
    return False


# Upper bound for OPS_AGENT_WAKE_TIMEOUT: a typo such as 100000 must not turn
# the deadline back into "wait for ever".
_WAKE_TIMEOUT_MAX = 3600.0


def _wake_timeout() -> float:
    """Seconds one agent wake may wait for its provider.

    ``OPS_AGENT_WAKE_TIMEOUT`` overrides it; anything that is not a positive
    number falls back to the brain's own deadline (``BRAIN_LLM_TIMEOUT``).
    """
    default = float(brain.LLM_TIMEOUT)
    raw = (os.environ.get("OPS_AGENT_WAKE_TIMEOUT") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not value > 0:
        return default
    return min(value, _WAKE_TIMEOUT_MAX)


def _skills_of(agent_id: int, task: str) -> str:
    """The skills installed for this agent, as a prompt block.

    '' when the agent has none, or when the skills cannot be read: a broken
    skill must not stop the agent from waking up.
    """
    try:
        from .skills import packs

        return packs.agent_skills_block(agent_id, task=task)
    except Exception:
        log.warning("could not read the skills of agent %s", agent_id, exc_info=True)
        return ""


def _wake_agent(agent_id: int, prompt: str, trigger_id: int | None = None) -> dict:
    """Wake an agent: fetch member + memory + chain, ask brain, log result."""
    t0 = time.time()
    log_id = db.log_agent_action(agent_id, "wake", trigger_id=trigger_id,
                                  summary=(prompt or "")[:200])
    try:
        member = db.get_member(agent_id)
        if not member:
            raise ValueError(f"agent {agent_id} not found")
        # Build system prompt (similar to _member_chat)
        name = member.get("name", "Agent")
        role = member.get("role") or "agent"
        level = member.get("level") or "agent"
        caps = member.get("capabilities") or ""
        model = member.get("model")
        chain = db.get_member_chain(agent_id)
        chain_str = " → ".join(m.get("name") for m in chain) or name
        memory_lines = db.top_agent_memory(agent_id, k=15)
        memory_str = "\n".join(memory_lines) if memory_lines else "(no memories yet)"

        owner = os.environ.get("OPS_OWNER_NAME", "the operator")
        system_prompt = (
            f"You are {name}, role={role}, level={level} in {owner}'s 24/7 ops organization.\n"
            f"Reporting chain (self → top): {chain_str}\n"
            f"Capabilities: {caps or '(none specified)'}\n"
            f"Model: {model or 'auto'}\n"
            f"\n"
            f"## Your memory (top items, by importance)\n{memory_str}\n"
            f"\n"
            f"## Behavior\n"
            f"You were woken automatically by a trigger. Do the work, log outcomes to "
            f"memory if something should persist, and respond concisely. Today's date: "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        # The agent wakes up as itself: the skills of its pack are the
        # criteria it judges the work by. Without them it was a generic
        # assistant with a role in a prompt.
        skills_block = _skills_of(agent_id, prompt)
        if skills_block:
            system_prompt += "\n\n## Your skills\n" + skills_block

        # Resolve provider from agent.model ("provider:model" syntax)
        provider_name = None
        if model and ":" in model:
            provider_name = model.split(":", 1)[0]
        prov = brain.resolve_provider(provider_name)
        if not prov.available():
            # Never fake a run: a mock answer was logged as a successful
            # 'run_complete' and persisted in agent_logs as if a real agent
            # had done the work. Fail loudly instead. Fixed 2026-10-23.
            raise brain.ProviderUnavailableError(
                provider_name or "auto", "unavailable",
                f"Provider '{provider_name or 'auto'}' is not available, so the agent "
                f"did not run. No output was produced.",
                model=model,
            )

        messages = [{"role": "user", "content": prompt or "(no prompt) — continue from memory"}]
        # The scheduler ticks on one thread: a call that never returns would
        # stop every later trigger and system job. The abandoned call is not
        # joined; whatever it returns later is dropped, not logged as a run.
        # Triggers and Telegram wakes get connectors + browse when the
        # provider supports tools (same path as team chat / Holding CEO).
        limit = _wake_timeout()
        try:
            import concurrent.futures as _cf
            ex = _cf.ThreadPoolExecutor(max_workers=1)
            fut = ex.submit(
                lambda: brain.ask_with_connector_tools(prov, system_prompt, messages)[0])
            try:
                text = fut.result(timeout=limit)
            except _cf.TimeoutError:
                raise TimeoutError(
                    f"{getattr(prov, 'name', 'llm')} exceeded {limit:.0f}s deadline") from None
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
        except TimeoutError:
            duration = int((time.time() - t0) * 1000)
            msg = (f"the provider did not answer within {limit:g}s, so the agent did not "
                   f"run. No output was produced.")
            db.log_agent_action(agent_id, "error", trigger_id=trigger_id,
                                summary=msg[:200], duration_ms=duration)
            log.warning("agent %s wake timed out after %ss", agent_id, limit)
            return {"ok": False, "error": msg, "timeout": True,
                    "duration_ms": duration, "log_id": log_id}

        # Log completion
        duration = int((time.time() - t0) * 1000)
        db.log_agent_action(agent_id, "run_complete", trigger_id=trigger_id,
                            task_id=None, summary=text[:200],
                            details={"prompt": prompt, "response": text[:1000]},
                            duration_ms=duration)
        return {"ok": True, "response": text, "duration_ms": duration, "log_id": log_id}
    except brain.ProviderUnavailableError as e:
        # Explicit agent provider cooling down: log a stable, parseable line
        # (the UI renders it as "Provider X is unavailable…") and return the
        # same payload the HTTP endpoints use.
        duration = int((time.time() - t0) * 1000)
        db.log_agent_action(agent_id, "error", trigger_id=trigger_id,
                            summary=provider_errors.run_error_text(e)[:200],
                            duration_ms=duration)
        return {**e.payload(), "log_id": log_id}
    except Exception as e:
        duration = int((time.time() - t0) * 1000)
        db.log_agent_action(agent_id, "error", trigger_id=trigger_id,
                            summary=str(e)[:200], duration_ms=duration)
        log.exception("agent %s wake failed", agent_id)
        return {"ok": False, "error": str(e), "log_id": log_id}


def tick(now: datetime | None = None) -> list[dict]:
    """Run one scheduler pass. Returns the list of fired triggers."""
    now = now or datetime.now()
    minute_key = now.strftime("%Y-%m-%d %H:%M")
    results = []
    triggers = db.list_all_enabled_triggers()
    for t in triggers:
        if t["kind"] != "cron":
            continue
        # Skip if already fired this minute
        if _fired_this_minute.get(t["id"]) == minute_key:
            continue
        if not _cron_matches(t["spec"], now):
            continue
        if not _should_fire(f"trigger:{t['id']}", _fired_this_minute, t["id"], minute_key):
            continue
        # Fire
        log.info("firing trigger %s for agent %s", t["id"], t["agent_id"])
        r = _wake_agent(t["agent_id"], t.get("prompt") or "(scheduled)", trigger_id=t["id"])
        if r["ok"]:
            db.record_trigger_fire(t["id"], status="ok")
        else:
            db.record_trigger_fire(t["id"], status="error", error=r.get("error"))
        results.append({"trigger_id": t["id"], "agent_id": t["agent_id"], **r})
    # Built-in system jobs (briefing, vault export) on their own crons.
    results.extend(run_system_jobs(now))
    # The jobs that missed their slot while the app was not looking (the Mac
    # asleep, the app closed): one per tick, so they do not all pile up at
    # once when it wakes up.
    results.extend(catch_up_system_jobs(now))
    # Media jobs: a video render that nobody polls is paid for and lost, so
    # every tick finishes the ones that are ready (2026-10-23).
    try:
        from . import media as _media_mod
        reaped = _media_mod.reap_jobs()
        if reaped.get("checked"):
            log.info("media reaper: %s", reaped)
            results.append({"media": reaped})
    except Exception:
        log.debug("media reaper failed", exc_info=True)
    return results


def run_loop(stop_event: threading.Event, interval: float = 30.0) -> None:
    """Background loop: tick every `interval` seconds."""
    log.info("scheduler started, interval=%.1fs", interval)
    while not stop_event.is_set():
        try:
            tick()
        except Exception:
            log.exception("scheduler tick failed")
        stop_event.wait(interval)
    log.info("scheduler stopped")


class Scheduler:
    """Background scheduler thread, started/stopped by the server."""

    def __init__(self, interval: float = 30.0):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=run_loop, args=(self._stop, self.interval),
                                         daemon=True, name="agent-scheduler")
        self._thread.start()
        # Chat channels (Telegram) live and die with the scheduler. They are
        # off by default and make no network call until the user turns one on.
        try:
            from . import channels
            channels.start()
        except Exception:
            log.exception("chat channels did not start")

    def stop(self) -> None:
        self._stop.set()
        try:
            from . import channels
            channels.stop()
        except Exception:
            pass
