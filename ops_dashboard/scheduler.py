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

import logging
import os
import threading
import time
from datetime import datetime

from . import brain, db, provider_errors

log = logging.getLogger("scheduler")

# Cache of "did this cron already fire this minute?" — prevents duplicate fires
# when the scheduler ticks twice within the same minute.
_fired_this_minute: dict[int, str] = {}  # trigger_id -> minute string
_sys_fired: dict[str, str] = {}          # system-job name -> minute string


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
        return {
            "ga": {k: ga_res.get(k) for k in ("sites", "fetched", "upserted")},
            "gsc": {"sites": len(gsc_res),
                    "fetched": sum(r.get("fetched", 0) for r in gsc_res),
                    "errors": [r["site"] for r in gsc_res if not r.get("ok")]},
        }

    def _learning():
        # L8: failure-log retention (D6) + skills curator that only PROPOSES
        # archiving skills unused for 60 days (D14). Never moves or deletes.
        from .learning import metrics
        return metrics.scheduled_maintenance()

    return [
        ("learning_maintenance", os.environ.get("LEARNING_MAINTENANCE_CRON", "15 4 * * *"), _learning),
        ("briefing", os.environ.get("BRIEFING_CRON", "0 8 * * *"), _briefing),
        ("obsidian_export", os.environ.get("OBSIDIAN_EXPORT_CRON", "0 3 * * *"), _export),
        ("analytics_poll", os.environ.get("ANALYTICS_POLL_CRON", "30 7 * * *"), _analytics),
    ]


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
            _sys_fired[name] = minute_key
        try:
            fn()
            out.append({"system_job": name, "ok": True})
        except Exception as e:
            log.exception("system job %s failed", name)
            out.append({"system_job": name, "ok": False, "error": str(e)})
    return out


def _cron_matches(expr: str, now: datetime) -> bool:
    """Match a 5-field cron expression against `now`. Supports *, */N, N, N-M, N,M."""
    try:
        fields = expr.split()
        if len(fields) != 5:
            return False
        minute, hour, dom, month, dow = fields
        vals = [now.minute, now.hour, now.day, now.month, now.weekday()]
        for f, v in zip([minute, hour, dom, month, dow], vals):
            if not _match_field(f, v, dow_field=(f is dow)):
                return False
        return True
    except Exception:
        return False


def _match_field(field: str, value: int, dow_field: bool = False) -> bool:
    """Match a single cron field. value: minute 0-59, hour 0-23, dom 1-31, month 1-12, dow 0-6 (Mon=0)."""
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            step = int(step)
            if base == "*":
                start, end = 0, 59 if not dow_field else 6
            elif "-" in base:
                start, end = (int(x) for x in base.split("-", 1))
            else:
                start = int(base); end = 59 if not dow_field else 6
            if value in range(start, end + 1, step):
                return True
        elif "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
            if start <= value <= end:
                return True
        elif part == "*":
            return True
        elif part.isdigit():
            if int(part) == value:
                return True
        elif dow_field and part.upper() in ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"):
            days = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
            if days[part.upper()] == value:
                return True
    return False


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

        # Resolve provider from agent.model ("provider:model" syntax)
        provider_name = None
        if model and ":" in model:
            provider_name = model.split(":", 1)[0]
        prov = brain.resolve_provider(provider_name)
        if not prov.available():
            prov = brain.resolve_provider("mock")

        messages = [{"role": "user", "content": prompt or "(no prompt) — continue from memory"}]
        text = prov.ask(system_prompt, messages)

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
        _fired_this_minute[t["id"]] = minute_key
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

    def stop(self) -> None:
        self._stop.set()
