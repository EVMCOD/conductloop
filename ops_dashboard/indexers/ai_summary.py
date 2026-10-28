"""Daily AI summary — generate an executive summary of yesterday's activity.

Uses the brain's LLM (Anthropic/OpenAI/Ollama) to write a 3-paragraph exec
summary of what happened, with focus on:
- Revenue changes (WoW) and project breakdown
- Traffic anomalies or notable shifts in GA
- Sentry issues spike (new errors or big event-count)
- Anomalies detected (URLs down, drops)
- Goals / tasks progress

Saves to $OPS_VAULT_ROOT/_agents/daily-summaries/YYYY-MM-DD.md
(markdown with frontmatter so it's indexed in the brain too).

Used by:
- /api/digest/ai-summary endpoint (manual regen)
- bin/run_digest.sh cron (after build_markdown)
- ops digest ai-summary CLI
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from .. import brain, db
from . import anomalies as anomalies_mod
from . import digest as digest_mod


def summary_dir() -> Path:
    """Where the daily summaries go: inside the vault chosen right now.

    Resolved when a summary is written, not at import: importing this module
    used to create ``~/Documents/vault/_agents`` on a machine that never chose it.
    """
    return db.vault_root() / "_agents" / "daily-summaries"


def _gather_day_context(target: dt.date) -> dict:
    """Pull the raw data the AI will summarize."""
    from .. import db
    day_start = int(dt.datetime.combine(target, dt.time.min).timestamp())
    day_end = day_start + 86400
    week_start = day_start - 7 * 86400
    prev_week_start = week_start - 7 * 86400

    ctx = {
        "date": target.isoformat(),
        "revenue": {
            "yesterday": db.revenue_summary(since=day_start, until=day_end),
            "last_7d": db.revenue_summary(since=week_start, until=day_end),
            "prev_7d": db.revenue_summary(since=prev_week_start, until=week_start),
        },
        "ga": {
            "yesterday": db.ga_daily_totals(since=target.isoformat(), until=target.isoformat()),
            "last_14d": db.ga_daily_totals(since=(target - dt.timedelta(days=14)).isoformat(), until=target.isoformat()),
        },
        "sentry": {
            "summary": db.sentry_summary(since=day_start - 14 * 86400),
            "new_today": db.list_sentry_issues(since=day_start, limit=10),
        },
        "anomalies": anomalies_mod.detect_recent_anomalies(),
        "urls": digest_mod._status_lines() if hasattr(digest_mod, "_status_lines") else None,
        "activity_recent": [
            {"kind": r["kind"], "summary": r["summary"], "project_key": r.get("project_key")}
            for r in db.recent_activity(limit=20)
            if r.get("created_at", 0) >= day_start - 86400
        ],
    }
    return ctx


def _format_context_for_prompt(ctx: dict) -> str:
    """Compact the context into a prompt-friendly summary."""
    lines = []
    rev_y = ctx["revenue"]["yesterday"]
    rev_w = ctx["revenue"]["last_7d"]
    rev_p = ctx["revenue"]["prev_7d"]
    lines.append(f"REVENUE — yesterday ({ctx['date']}): €{rev_y['total_cents']/100:,.2f} from {rev_y['count']} events")
    lines.append(f"  Last 7d: €{rev_w['total_cents']/100:,.2f} ({rev_w['count']} events)")
    if rev_p["total_cents"] > 0:
        wow = (rev_w["total_cents"] - rev_p["total_cents"]) / rev_p["total_cents"] * 100
        lines.append(f"  Prev 7d: €{rev_p['total_cents']/100:,.2f} → WoW {wow:+.1f}%")
    if rev_y["by_project"]:
        lines.append("  By project (yesterday):")
        for r in sorted(rev_y["by_project"], key=lambda x: -x["total"])[:5]:
            lines.append(f"    {r['project_key']}: €{r['total']/100:,.0f} ({r['cnt']} events)")
    if rev_y["by_type"]:
        lines.append("  By event type:")
        for r in rev_y["by_type"]:
            lines.append(f"    {r['event_type']}: €{r['total']/100:,.0f} ({r['cnt']})")

    ga_y = ctx["ga"]["yesterday"]
    if ga_y:
        u = sum(d["users"] or 0 for d in ga_y)
        s = sum(d["sessions"] or 0 for d in ga_y)
        pv = sum(d["page_views"] or 0 for d in ga_y)
        lines.append(f"\nGA4 TRAFFIC — yesterday: {u:,} users, {s:,} sessions, {pv:,} page views")
    ga_14 = ctx["ga"]["last_14d"]
    if ga_14:
        avg_u = sum(d["users"] or 0 for d in ga_14) / max(1, len(ga_14))
        lines.append(f"  14d avg: {avg_u:,.0f} users/day ({len(ga_14)} days)")

    sentry = ctx["sentry"]
    by_level_str = ", ".join(f"{r['level']}={r['cnt']}" for r in sentry['summary']['by_level'])
    lines.append(f"\nSENTRY — last 14d: {sentry['summary']['total']} issues ({by_level_str})")
    if sentry["new_today"]:
        lines.append(f"  Active today ({len(sentry['new_today'])}):")
        for it in sentry["new_today"][:5]:
            lines.append(f"    [{it['level']}] {it['title'][:70]} ({it['event_count']} events)")

    an = ctx["anomalies"]
    if an["anomalies"]:
        lines.append(f"\nANOMALIES — {an['count']} detected (high={an['by_severity']['high']}, medium={an['by_severity']['medium']}):")
        for a in an["anomalies"][:5]:
            sev = a["severity"][0].upper()
            lines.append(f"  [{sev}] {a.get('message')}")

    if ctx["activity_recent"]:
        lines.append(f"\nRECENT ACTIVITY (last {len(ctx['activity_recent'])} events):")
        for e in ctx["activity_recent"][:8]:
            lines.append(f"  [{e['kind']}] {e['summary'][:90]}")
    return "\n".join(lines)


SYSTEM_PROMPT = """You are ConductLoop's daily analyst. You write a 3-paragraph exec summary of yesterday's metrics, anomalies, and product activity for the user. Be direct, specific, and actionable. No fluff. No "Overall". Match the user's language. Use proper number formatting."""


USER_PROMPT_TEMPLATE = """Yesterday was {date}. Here is the raw data:

{context}

Write a 3-paragraph exec summary in Spanish. Be direct, specific.

Paragraph 1: "Lo que sucedió" — financial + traffic headline, 2-3 sentences with numbers
Paragraph 2: "Lo que necesita atención" — anomalies, new errors, anything broken — be specific
Paragraph 3: "Lo que deberías hacer mañana" — 1-2 concrete actions based on the data

On the very last line, output ONLY this emoji: 🟢 or 🟡 or 🔴
(🟢 if everything normal, 🟡 if some anomalies, 🔴 if major issues). No other text after the emoji."""


def generate_ai_summary(target: dt.date | None = None, *, provider_name: str | None = None) -> dict:
    """Call the LLM to summarize the day's activity. Returns {markdown, model, path}."""
    target = target or (dt.date.today() - dt.timedelta(days=1))
    ctx = _gather_day_context(target)
    context_str = _format_context_for_prompt(ctx)
    prompt = USER_PROMPT_TEMPLATE.format(date=target.isoformat(), context=context_str)

    try:
        provider = brain.resolve_provider(provider_name)
        summary_text = provider.ask(SYSTEM_PROMPT, [{"role": "user", "content": prompt}])
        if not summary_text or not summary_text.strip():
            summary_text = "_(AI returned empty response — provider may not be configured)_"
        model_name = getattr(provider, "model", "unknown") or "unknown"
    except brain.ProviderUnavailableError:
        raise  # explicit provider cooling down: caller shows provider_unavailable
    except Exception as e:
        summary_text = f"_(AI summary failed: {e})_"
        model_name = "error"

    # Save to file with frontmatter
    out_dir = summary_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{target.isoformat()}.md"
    frontmatter = (
        f"---\n"
        f"date: {target.isoformat()}\n"
        f"generated_at: {dt.datetime.now().isoformat(timespec='seconds')}\n"
        f"model: {model_name}\n"
        f"revenue_yesterday_cents: {ctx['revenue']['yesterday']['total_cents']}\n"
        f"revenue_7d_cents: {ctx['revenue']['last_7d']['total_cents']}\n"
        f"ga_users_yesterday: {sum(d.get('users', 0) or 0 for d in ctx['ga']['yesterday'])}\n"
        f"anomalies_count: {ctx['anomalies']['count']}\n"
        f"---\n\n"
    )
    full_md = frontmatter + f"# AI daily summary — {target.isoformat()}\n\n" + summary_text + "\n"
    out_path.write_text(full_md, encoding="utf-8")

    # Log to activity feed
    try:
        from .. import db as _db
        _db.add_activity("ai_summary", f"Daily AI summary for {target.isoformat()} (model: {model_name})")
    except Exception:
        pass
    return {
        "date": target.isoformat(),
        "path": str(out_path),
        "markdown": full_md,
        "model": model_name,
        "context": ctx,
    }


def run(target: dt.date | None = None, *, provider_name: str | None = None, target_date: dt.date | None = None, **kwargs) -> dict:
    """CLI entry point. Returns the same dict as generate_ai_summary.
    Accepts both `target` and `target_date` keyword args for backwards compat."""
    actual = target or target_date
    return generate_ai_summary(actual, provider_name=provider_name)
