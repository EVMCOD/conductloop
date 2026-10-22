"""Daily digest — generate a morning summary of yesterday's activity.

For each run, builds a markdown summary covering:
- Revenue (yesterday + WoW change, top projects)
- GA traffic (yesterday, top sites, anomalies)
- URL health (which URLs are down)
- Anomalies (revenue/GA spikes/drops)
- Top 5 ideas/decisions to revisit

Output: markdown string (suitable for email body, Obsidian note, or webhook payload).
Saved to <data dir>/digests/YYYY-MM-DD.md (app_paths).
Sent to configured webhook (Slack/Discord/Telegram) if env vars set.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from .. import app_paths as _app_paths
from .. import db, webhooks
from . import anomalies as anomalies_mod

DIGEST_DIR = _app_paths.path("digests", "data/digests")
DIGEST_DIR.mkdir(parents=True, exist_ok=True)


def _fmt_eur(cents: int | None) -> str:
    if cents is None: return "—"
    return f"€{cents/100:,.2f}"


def _pct(curr: float, base: float) -> str:
    if base == 0: return "—"
    pct = (curr - base) / base * 100
    arrow = "↑" if pct > 0 else "↓"
    return f"{arrow}{pct:+.1f}%"


def build_markdown(target_date: dt.date | None = None) -> str:
    """Build the daily digest markdown for the given day (default: yesterday)."""
    target = target_date or (dt.date.today() - dt.timedelta(days=1))
    target_iso = target.isoformat()
    day_start = int(dt.datetime.combine(target, dt.time.min).timestamp())
    day_end = day_start + 86400
    week_start = day_start - 7 * 86400

    lines: list[str] = []
    lines.append(f"# Daily digest — {target_iso}")
    lines.append("")
    lines.append(f"_Generated at {dt.datetime.now().isoformat(timespec='seconds')}_")
    lines.append("")

    # ----- REVENUE -----
    lines.append("## 💰 Revenue")
    rev = db.revenue_summary(since=day_start, until=day_end)
    rev_week = db.revenue_summary(since=week_start, until=day_end)
    rev_prev_week = db.revenue_summary(since=week_start - 7 * 86400, until=week_start)
    lines.append(f"- **Yesterday**: {_fmt_eur(rev['total_cents'])} from {rev['count']} events")
    lines.append(f"- **Last 7 days total**: {_fmt_eur(rev_week['total_cents'])} (vs prev 7d {_pct(rev_week['total_cents'], rev_prev_week['total_cents'])})")
    if rev["by_project"]:
        lines.append("- **By project (yesterday)**:")
        for r in sorted(rev["by_project"], key=lambda x: -x["total"])[:5]:
            lines.append(f"  - {r['project_key']}: {_fmt_eur(r['total'])} ({r['cnt']} events)")
    lines.append("")

    # ----- GA -----
    lines.append("## 📈 Traffic (GA4)")
    ga_today = db.ga_daily_totals(since=target_iso, until=target_iso)
    ga_week = db.ga_daily_totals(since=(target - dt.timedelta(days=7)).isoformat(), until=target_iso)
    if ga_today:
        users = sum(d["users"] or 0 for d in ga_today)
        sessions = sum(d["sessions"] or 0 for d in ga_today)
        pv = sum(d["page_views"] or 0 for d in ga_today)
        lines.append(f"- **Yesterday**: {users:,} users · {sessions:,} sessions · {pv:,} page views")
    else:
        lines.append("- **Yesterday**: no data")
    week_users = sum(d["users"] or 0 for d in ga_week)
    week_sessions = sum(d["sessions"] or 0 for d in ga_week)
    lines.append(f"- **Last 7 days total**: {week_users:,} users · {week_sessions:,} sessions")
    # Per-site yesterday
    today_metrics = db.list_ga_metrics_daily(since=target_iso, until=target_iso)
    if today_metrics:
        per_site = {}
        for m in today_metrics:
            per_site.setdefault(m["project_key"], {"users": 0, "page_views": 0})
            per_site[m["project_key"]]["users"] += m["users"] or 0
            per_site[m["project_key"]]["page_views"] += m["page_views"] or 0
        lines.append("- **By site (yesterday)**:")
        for proj, s in sorted(per_site.items(), key=lambda x: -x[1]["users"])[:5]:
            lines.append(f"  - {proj}: {s['users']:,} users · {s['page_views']:,} page views")
    lines.append("")

    # ----- URL HEALTH -----
    lines.append("## 🟢 URL health")
    try:
        from . import live_urls
        unhealthy = live_urls.recent_unhealthy(hours=24)
        if unhealthy:
            lines.append(f"- ⚠️ **{len(unhealthy)} unhealthy in last 24h**:")
            for r in unhealthy:
                lines.append(f"  - {r.get('name')} ({r.get('url')}): HTTP {r.get('status_code') or 'no response'}")
        else:
            lines.append("- ✅ All known URLs healthy")
    except Exception as e:
        lines.append(f"- (URL check failed: {e})")
    lines.append("")

    # ----- ANOMALIES -----
    lines.append("## 🚨 Anomalies")
    try:
        an = anomalies_mod.detect_recent_anomalies()
        if an["anomalies"]:
            lines.append(f"- **{an['count']} detected** (high: {an['by_severity']['high']}, medium: {an['by_severity']['medium']})")
            for a in an["anomalies"][:8]:
                sev = "🔴" if a["severity"] == "high" else ("🟡" if a["severity"] == "medium" else "🔵")
                lines.append(f"  - {sev} {a.get('message')}")
        else:
            lines.append("- ✅ No anomalies detected")
    except Exception as e:
        lines.append(f"- (anomaly check failed: {e})")
    lines.append("")

    # ----- MARGIN SNAPSHOT -----
    lines.append("## 💹 Margin snapshot (all time)")
    try:
        mrows = db.revenue_margin_by_project()
        active = [r for r in mrows if r["revenue_cents"] > 0]
        if active:
            lines.append("| project | revenue | costs | margin | % |")
            lines.append("|---|---:|---:|---:|---:|")
            for r in active[:8]:
                pct = f"{r['margin_pct']:.1f}%" if r.get("margin_pct") is not None else "—"
                lines.append(f"| {r['project_key']} | {_fmt_eur(r['revenue_cents'])} | {_fmt_eur(r['cost_cents'])} | {_fmt_eur(r['margin_cents'])} | {pct} |")
    except Exception:
        pass
    lines.append("")

    # ----- FOOTER -----
    lines.append("---")
    lines.append("_Sources: revenue_events · ga_metrics_daily · live_urls · costs_ · anomalies_")
    lines.append("_Dashboard: http://localhost:7777/_")
    lines.append("")
    return "\n".join(lines)


def save_digest(target_date: dt.date | None = None) -> Path:
    md = build_markdown(target_date)
    target = target_date or (dt.date.today() - dt.timedelta(days=1))
    out = DIGEST_DIR / f"{target.isoformat()}.md"
    out.write_text(md, encoding="utf-8")
    return out


def send_digest(markdown: str, *, target_date: dt.date | None = None) -> dict:
    """Send the digest via configured webhook (Slack/Discord/Telegram).

    Slack/Discord: uses webhooks.notify which honors SLACK_WEBHOOK_URL /
    DISCORD_WEBHOOK_URL env vars.
    Telegram: uses TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.

    Returns a dict of {channel: ok} so caller knows what succeeded.
    """
    target = target_date or (dt.date.today() - dt.timedelta(days=1))
    # webhooks.notify takes a single message; we just send the markdown as text.
    # Slack/Discord will render the first 200 chars as preview. For full
    # readability, we send title + first 500 chars.
    title = f"Daily digest {target.isoformat()}"
    summary = markdown[:600].replace("# ", "").replace("**", "").replace("\n", " | ")[:500]
    # Send via webhooks.notify (handles Slack/Discord/Telegram)
    res = webhooks.notify(f"{title}\n{summary}…", severity="info")
    # res is {channel: bool}; return a richer dict
    return {
        channel: {"ok": ok, "configured": True}
        for channel, ok in res.items()
    }


def run(target_date: dt.date | None = None, *, with_ai_summary: bool = True) -> dict:
    """Save + send the digest. Returns the saved path and send result.

    If with_ai_summary=True (default), also generates an AI-written exec summary
    via the brain provider and saves it to $OPS_VAULT_ROOT/_agents/daily-summaries/.
    """
    target = target_date or (dt.date.today() - dt.timedelta(days=1))
    path = save_digest(target)
    md = path.read_text(encoding="utf-8")
    send_result = send_digest(md, target_date=target)
    # AI summary (best-effort, don't fail the digest if this errors)
    ai_result = None
    if with_ai_summary:
        try:
            from . import ai_summary
            ai_result = ai_summary.generate_ai_summary(target_date=target)
        except Exception as e:
            ai_result = {"error": str(e)}
    # Log to activity feed
    try:
        from .. import db as _db
        channels_ok = sum(1 for v in (send_result or {}).values() if isinstance(v, dict) and v.get("ok"))
        summary_msg = f"Daily digest for {target.isoformat()} (sent to {channels_ok} channels"
        if ai_result and not ai_result.get("error"):
            summary_msg += f", AI summary by {ai_result.get('model', '?')}"
        summary_msg += ")"
        _db.add_activity("digest_sent", summary_msg)
    except Exception:
        pass
    return {"date": target.isoformat(), "path": str(path), "send": send_result, "ai_summary": ai_result}
