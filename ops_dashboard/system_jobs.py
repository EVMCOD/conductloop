"""What state is each of the app's own jobs in.

The scheduler runs the built-in jobs on their cron. This module reads what
happened and turns it into something a person can act on:

- ``ok`` — the last run worked.
- ``error`` — the last run failed. The error is kept, because "the backup
  failed" is only half an answer: "the backup failed because the disk is
  full" is the answer.
- ``never`` — the job has not run yet (a fresh install, or the scheduler
  has never been up).
- ``overdue`` — the last run worked, but it should have run again by now
  and has not. This is the failure that used to be invisible: the app was
  not doing its work and looked perfectly healthy.

The names of the jobs are stable keys, not words: the panel names them in
the language the person chose, without the server having to know it.
"""
from __future__ import annotations

import json
import time

# The words of the messages the server writes about its own jobs. The panel
# shows them as they are, so they are written here in the language of the
# person, like the words of the anomaly alerts.
_WORDS: dict[str, dict[str, str]] = {
    "en": {
        "backup": "Backup",
        "learning_maintenance": "Learning maintenance",
        "briefing": "Morning briefing",
        "obsidian_export": "Vault export",
        "analytics_poll": "Analytics poll",
        "library_index": "Library index",
        "daily_note": "Daily note",
        "failed": "{name} failed: {error}",
        "late": "{name} did not run when it should have",
        "yes": "yes",
        "no": "no",
        # What a run says it did.
        "sum_sites": "sites",
        "sum_fetched": "rows fetched",
        "sum_upserted": "rows saved",
        "sum_indexed": "notes indexed",
        "sum_skipped": "left as they were",
        "sum_errors": "files that could not be read",
        "sum_proposed": "proposals",
        "sum_purged_failures": "old failures forgotten",
        "sum_curator": "curator",
        "sum_memory": "memory",
        "sum_decisions": "decisions",
        "sum_obsidian": "vault",
        "sum_note": "note",
        "sum_daily_note": "daily note",
        "sum_ga": "Analytics",
        "sum_gsc": "Search Console",
        "sum_backup": "backup",
        "sum_newest": "newest backup",
        "sum_telegram": "Telegram",
        "sum_discord": "Discord",
        # The same key can mean different things in different jobs, so the
        # label follows the type of the value: a list of sites that failed is
        # not a count of files that could not be read.
        "sum_errors_list": "sites that failed",
        "sum_skipped_bool": "skipped",
        "sum_imported": "imported",
        "sum_considered": "considered",
        "sum_assigned": "assigned",
        "sum_repos": "repositories",
        "sum_structure": "structure",
    },
    "es": {
        "backup": "Copia de seguridad",
        "learning_maintenance": "Mantenimiento del aprendizaje",
        "briefing": "Briefing de la mañana",
        "obsidian_export": "Exportación a la bóveda",
        "analytics_poll": "Consulta de analítica",
        "library_index": "Índice de la biblioteca",
        "daily_note": "Nota diaria",
        "failed": "{name} falló: {error}",
        "late": "{name} no corrió cuando debía",
        "yes": "sí",
        "no": "no",
        # Lo que dice una corrida que hizo.
        "sum_sites": "sitios",
        "sum_fetched": "filas bajadas",
        "sum_upserted": "filas guardadas",
        "sum_indexed": "notas indexadas",
        "sum_skipped": "sin tocar",
        "sum_errors": "ficheros que no se pudieron leer",
        "sum_proposed": "propuestas",
        "sum_purged_failures": "fallos antiguos olvidados",
        "sum_curator": "curador",
        "sum_memory": "memoria",
        "sum_decisions": "decisiones",
        "sum_obsidian": "bóveda",
        "sum_note": "nota",
        "sum_daily_note": "nota diaria",
        "sum_ga": "Analytics",
        "sum_gsc": "Search Console",
        "sum_backup": "copia",
        "sum_newest": "última copia",
        "sum_telegram": "Telegram",
        "sum_discord": "Discord",
        # La misma clave puede querer decir cosas distintas en trabajos
        # distintos, así que la etiqueta sigue al tipo del valor: una lista
        # de sitios que fallaron no es una cuenta de ficheros ilegibles.
        "sum_errors_list": "sitios que fallaron",
        "sum_skipped_bool": "omitido",
        "sum_imported": "importados",
        "sum_considered": "consideradas",
        "sum_assigned": "asignadas",
        "sum_repos": "repositorios",
        "sum_structure": "estructura",
    },
}

# The grace period lives with the schedule, in the scheduler: the panel and
# the recovery ask the same question and have to get the same answer.


def _words() -> dict[str, str]:
    """The words of the job messages, in the language of the person."""
    from . import i18n

    try:
        return _WORDS[i18n.current_locale()]
    except Exception:
        return _WORDS["en"]


def name_of(job: str) -> str:
    """The name of a job, in the language of the person."""
    words = _words()
    return words.get(job) or job.replace("_", " ").capitalize()


def message_of(job: str, state: str, error: str | None = None) -> str:
    """What happened to a job, in the language of the person."""
    words = _words()
    template = words.get("failed" if state == "error" else "late")
    if not template:
        return name_of(job)
    text = template.format(name=name_of(job), error=error or "")
    return text.strip()


def _slot_missed(spec: str, last_run_at: int, now: int) -> bool:
    """Whether the job missed a slot it should have run in.

    The schedule knows: it is the same question the scheduler asks when it
    decides to catch up, so the panel and the recovery always agree.
    """
    from . import scheduler

    return scheduler.slot_missed(spec, last_run_at, now)


def _state_of(last: dict | None, spec: str, now: int) -> str:
    if not last:
        return "never"
    if last.get("status") != "ok":
        return "error"
    if _slot_missed(spec, int(last.get("ran_at") or 0), now):
        return "overdue"
    return "ok"


def summary_text(raw: str | None) -> str:
    """What the run said it did, in the language of the person.

    "12 rows saved", "3 sites failed": the numbers are the job's, the words
    are the reader's. A job that reports several things keeps them
    together: "Analytics · sites: 3, rows saved: 12; Search Console · ...".
    """
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        return ""
    words = _words()
    groups: dict[str, list[str]] = {}
    _collect(data, words, groups, "")
    parts = []
    for prefix, items in groups.items():
        text = ", ".join(items)
        parts.append(f"{prefix} · {text}" if prefix else text)
    return "; ".join(parts)


def _collect(data, words: dict, groups: dict, prefix: str = "") -> None:
    """The readable pieces of a job's result.

    The labels are per key, and the same key can mean different things in
    different jobs: "errors" is a count of files the index could not read
    and a list of the sites that failed. So the label follows the type of
    the value, and a job that nests its result keeps the name of the job
    in front of what it did ("Analytics · sites: 3").
    """
    if isinstance(data, dict):
        for key, value in data.items():
            label = words.get(f"sum_{key}")
            if label is None:
                continue
            if isinstance(value, bool):
                label = words.get(f"sum_{key}_bool") or label
                _say(groups, prefix, f"{label}: "
                     f"{words.get('yes') if value else words.get('no')}")
            elif isinstance(value, dict):
                _collect(value, words, groups, label)
            elif isinstance(value, (int, float)):
                _say(groups, prefix, f"{label}: {value}")
            elif isinstance(value, (list, tuple)):
                label = words.get(f"sum_{key}_list") or label
                _say(groups, prefix, f"{label}: {len(value)}")
            elif isinstance(value, str) and value.strip():
                _say(groups, prefix, f"{label}: {_short(value)}")
    elif isinstance(data, (list, tuple)):
        for item in data:
            _collect(item, words, groups, prefix)


def _say(groups: dict, prefix: str, text: str) -> None:
    groups.setdefault(prefix, []).append(text)


def _short(value: str) -> str:
    """The name of a file, not the way to it.

    A job that writes a note says where; the panel says which one.
    """
    if "/" in value:
        return value.rsplit("/", 1)[-1] or value
    return value


def overview(*, now: int | None = None) -> dict:
    """Every built-in job, with its cron and how it is doing.

    The jobs are read from the scheduler, so a job that is added there
    shows up here without anybody having to remember to add it twice.
    """
    from . import scheduler
    from .db_jobs import last_system_job_runs, system_job_failure_count

    now = int(now if now is not None else time.time())
    lasts = last_system_job_runs()
    jobs = []
    for name, spec in scheduler.system_job_specs():
        last = lasts.get(name)
        state = _state_of(last, spec, now)
        job = {
            "name": name,
            "label": name_of(name),
            "cron": spec,
            "state": state,
            "last": _last_view(last),
            "failures_7d": system_job_failure_count(name),
        }
        if state in ("error", "overdue"):
            job["message"] = message_of(name, state, (last or {}).get("error"))
        did = summary_text((last or {}).get("summary"))
        if did:
            job["did"] = did
        jobs.append(job)
    counts = {"ok": 0, "error": 0, "never": 0, "overdue": 0}
    for job in jobs:
        counts[job["state"]] = counts.get(job["state"], 0) + 1
    return {
        "ok": True,
        "generated_at": now,
        "jobs": jobs,
        "counts": counts,
        # The jobs that want a look: the ones that failed and the ones that
        # stopped running. A job that never ran is not an alarm on a fresh
        # install; an overdue one always is.
        "needs_attention": [j["name"] for j in jobs
                             if j["state"] in ("error", "overdue")],
    }


def _last_view(last: dict | None) -> dict | None:
    if not last:
        return None
    return {
        "status": last.get("status"),
        "error": last.get("error"),
        "duration_ms": last.get("duration_ms"),
        "ran_at": last.get("ran_at"),
        # Why it ran: its schedule, the slot it missed while the app was
        # closed, or the person asking for it.
        "kind": last.get("kind") or "scheduled",
    }


def one(name: str, *, now: int | None = None) -> dict | None:
    """The state of one job, by name.

    ``now`` is the moment to judge it by. It is there for the tests: a
    job that runs at 08:00 is late at 09:00 and not late at 07:00, and a
    test that reads the wall clock says one thing at breakfast and another
    at midnight.
    """
    for job in overview(now=now)["jobs"]:
        if job["name"] == name:
            return job
    return None
