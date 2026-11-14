"""Daily digest — generate a morning summary of yesterday's activity.

For each run, builds a markdown summary covering:
- Revenue (yesterday + WoW change, top projects)
- GA traffic (yesterday, top sites, anomalies)
- URL health (which URLs are down)
- Anomalies (revenue/GA spikes/drops)
- Top 5 ideas/decisions to revisit

Output: markdown string (suitable for email body, Obsidian note, or webhook payload).
Saved to <data dir>/digests/YYYY-MM-DD.md (app_paths).
Sent to configured webhook (Slack/Discord) if env vars set.

The words are the app's: the digest is a document for the person, so it is
written in the language they chose.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from .. import app_paths as _app_paths
from .. import db, webhooks
from . import anomalies as anomalies_mod

DIGEST_DIR = _app_paths.path("digests", "data/digests")
DIGEST_DIR.mkdir(parents=True, exist_ok=True)

# The words of the digest, in the language the person chose. The module that
# owns the words owns the dictionary.
_WORDS: dict[str, dict[str, str]] = {
    "en": {
        "title": "# Daily digest — {date}",
        "generated": "_Generated at {ts}_",
        "revenue_title": "## 💰 Revenue",
        "yesterday": "Yesterday",
        "from_events": "{amount} from {n} events",
        "last_7d": "Last 7 days total",
        "vs_prev": "(vs prev 7d {pct})",
        "by_project": "By project (yesterday)",
        "project_line": "{project}: {amount} ({n} events)",
        "traffic_title": "## 📈 Traffic (GA4)",
        "traffic_yesterday": "{users} users · {sessions} sessions · {pv} page views",
        "traffic_7d": "{users} users · {sessions} sessions",
        "no_data": "no data",
        "by_site": "By site (yesterday)",
        "site_line": "{site}: {users} users · {pv} page views",
        "url_title": "## 🟢 URL health",
        "unhealthy": "{n} unhealthy in last 24h",
        "url_line": "{name} ({url}): HTTP {code}",
        "all_healthy": "All known URLs healthy",
        "url_check_failed": "URL check failed: {err}",
        "anomalies_title": "## 🚨 Anomalies",
        "detected": "{n} detected (high: {high}, medium: {medium})",
        "no_anomalies": "No anomalies detected",
        "anomaly_check_failed": "anomaly check failed: {err}",
        "margin_title": "## 💹 Margin snapshot (all time)",
        "table_head": "| project | revenue | costs | margin | % |",
        "sources": "_Sources: revenue_events · ga_metrics_daily · live_urls · costs_ · anomalies_",
        "dashboard": "_Dashboard: http://localhost:7777/_",
        "send_title": "Daily digest {date}",
        "activity": "Daily digest for {date} (sent to {n} channels",
        "activity_ai": ", AI summary by {model}",
        "activity_end": ")",
    },
    "es": {
        "title": "# Digest diario — {date}",
        "generated": "_Generado el {ts}_",
        "revenue_title": "## 💰 Ingresos",
        "yesterday": "Ayer",
        "from_events": "{amount} de {n} eventos",
        "last_7d": "Total de los últimos 7 días",
        "vs_prev": "(vs 7d anteriores {pct})",
        "by_project": "Por proyecto (ayer)",
        "project_line": "{project}: {amount} ({n} eventos)",
        "traffic_title": "## 📈 Tráfico (GA4)",
        "traffic_yesterday": "{users} usuarios · {sessions} sesiones · {pv} páginas vistas",
        "traffic_7d": "{users} usuarios · {sessions} sesiones",
        "no_data": "sin datos",
        "by_site": "Por sitio (ayer)",
        "site_line": "{site}: {users} usuarios · {pv} páginas vistas",
        "url_title": "## 🟢 Salud de las URLs",
        "unhealthy": "{n} con problemas en las últimas 24 h",
        "url_line": "{name} ({url}): HTTP {code}",
        "all_healthy": "Todas las URLs conocidas están bien",
        "url_check_failed": "no se pudieron comprobar las URLs: {err}",
        "anomalies_title": "## 🚨 Anomalías",
        "detected": "{n} detectadas (altas: {high}, medias: {medium})",
        "no_anomalies": "No se detectaron anomalías",
        "anomaly_check_failed": "no se pudieron comprobar las anomalías: {err}",
        "margin_title": "## 💹 Margen (todo el histórico)",
        "table_head": "| proyecto | ingresos | costes | margen | % |",
        "sources": "_Fuentes: revenue_events · ga_metrics_daily · live_urls · costs_ · anomalies_",
        "dashboard": "_Panel: http://localhost:7777/_",
        "send_title": "Digest diario {date}",
        "activity": "Digest diario del {date} (enviado a {n} canales",
        "activity_ai": ", resumen de IA por {model}",
        "activity_end": ")",
    },
}


def _words() -> dict[str, str]:
    """The words of the digest, in the language of the person."""
    from .. import i18n

    try:
        return _WORDS[i18n.current_locale()]
    except Exception:
        return _WORDS["en"]


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
    w = _words()
    target = target_date or (dt.date.today() - dt.timedelta(days=1))
    target_iso = target.isoformat()
    day_start = int(dt.datetime.combine(target, dt.time.min).timestamp())
    day_end = day_start + 86400
    week_start = day_start - 7 * 86400

    lines: list[str] = []
    lines.append(w["title"].format(date=target_iso))
    lines.append("")
    lines.append(w["generated"].format(ts=dt.datetime.now().isoformat(timespec="seconds")))
    lines.append("")

    # ----- REVENUE -----
    lines.append(w["revenue_title"])
    rev = db.revenue_summary(since=day_start, until=day_end)
    rev_week = db.revenue_summary(since=week_start, until=day_end)
    rev_prev_week = db.revenue_summary(since=week_start - 7 * 86400, until=week_start)
    lines.append(f"- **{w['yesterday']}**: "
                 + w["from_events"].format(amount=_fmt_eur(rev["total_cents"]), n=rev["count"]))
    lines.append(f"- **{w['last_7d']}**: {_fmt_eur(rev_week['total_cents'])} "
                 + w["vs_prev"].format(pct=_pct(rev_week["total_cents"], rev_prev_week["total_cents"])))
    if rev["by_project"]:
        lines.append(f"- **{w['by_project']}**:")
        for r in sorted(rev["by_project"], key=lambda x: -x["total"])[:5]:
            lines.append("  - " + w["project_line"].format(
                project=r["project_key"], amount=_fmt_eur(r["total"]), n=r["cnt"]))
    lines.append("")

    # ----- GA -----
    lines.append(w["traffic_title"])
    ga_today = db.ga_daily_totals(since=target_iso, until=target_iso)
    ga_week = db.ga_daily_totals(since=(target - dt.timedelta(days=7)).isoformat(), until=target_iso)
    if ga_today:
        users = sum(d["users"] or 0 for d in ga_today)
        sessions = sum(d["sessions"] or 0 for d in ga_today)
        pv = sum(d["page_views"] or 0 for d in ga_today)
        lines.append(f"- **{w['yesterday']}**: " + w["traffic_yesterday"].format(
            users=f"{users:,}", sessions=f"{sessions:,}", pv=f"{pv:,}"))
    else:
        lines.append(f"- **{w['yesterday']}**: {w['no_data']}")
    week_users = sum(d["users"] or 0 for d in ga_week)
    week_sessions = sum(d["sessions"] or 0 for d in ga_week)
    lines.append(f"- **{w['last_7d']}**: "
                 + w["traffic_7d"].format(users=f"{week_users:,}", sessions=f"{week_sessions:,}"))
    # Per-site yesterday
    today_metrics = db.list_ga_metrics_daily(since=target_iso, until=target_iso)
    if today_metrics:
        per_site = {}
        for m in today_metrics:
            per_site.setdefault(m["project_key"], {"users": 0, "page_views": 0})
            per_site[m["project_key"]]["users"] += m["users"] or 0
            per_site[m["project_key"]]["page_views"] += m["page_views"] or 0
        lines.append(f"- **{w['by_site']}**:")
        for proj, s in sorted(per_site.items(), key=lambda x: -x[1]["users"])[:5]:
            lines.append("  - " + w["site_line"].format(
                site=proj, users=f"{s['users']:,}", pv=f"{s['page_views']:,}"))
    lines.append("")

    # ----- URL HEALTH -----
    lines.append(w["url_title"])
    try:
        from . import live_urls
        unhealthy = live_urls.recent_unhealthy(hours=24)
        if unhealthy:
            lines.append(f"- ⚠️ **{w['unhealthy'].format(n=len(unhealthy))}**:")
            for r in unhealthy:
                lines.append("  - " + w["url_line"].format(
                    name=r.get("name"), url=r.get("url"), code=r.get("status_code") or "no response"))
        else:
            lines.append(f"- ✅ {w['all_healthy']}")
    except Exception as e:
        lines.append(f"- ({w['url_check_failed'].format(err=e)})")
    lines.append("")

    # ----- ANOMALIES -----
    lines.append(w["anomalies_title"])
    try:
        an = anomalies_mod.detect_recent_anomalies()
        if an["anomalies"]:
            lines.append(f"- **{w['detected'].format(n=an['count'], high=an['by_severity']['high'], medium=an['by_severity']['medium'])}**")
            for a in an["anomalies"][:8]:
                sev = "🔴" if a["severity"] == "high" else ("🟡" if a["severity"] == "medium" else "🔵")
                lines.append(f"  - {sev} {a.get('message')}")
        else:
            lines.append(f"- ✅ {w['no_anomalies']}")
    except Exception as e:
        lines.append(f"- ({w['anomaly_check_failed'].format(err=e)})")
    lines.append("")

    # ----- MARGIN SNAPSHOT -----
    lines.append(w["margin_title"])
    try:
        mrows = db.revenue_margin_by_project()
        active = [r for r in mrows if r["revenue_cents"] > 0]
        if active:
            lines.append(w["table_head"])
            lines.append("|---|---:|---:|---:|---:|")
            for r in active[:8]:
                pct = f"{r['margin_pct']:.1f}%" if r.get("margin_pct") is not None else "—"
                lines.append(f"| {r['project_key']} | {_fmt_eur(r['revenue_cents'])} | {_fmt_eur(r['cost_cents'])} | {_fmt_eur(r['margin_cents'])} | {pct} |")
    except Exception:
        pass
    lines.append("")

    # ----- FOOTER -----
    lines.append("---")
    lines.append(w["sources"])
    lines.append(w["dashboard"])
    lines.append("")
    return "\n".join(lines)


def save_digest(target_date: dt.date | None = None) -> Path:
    md = build_markdown(target_date)
    target = target_date or (dt.date.today() - dt.timedelta(days=1))
    out = DIGEST_DIR / f"{target.isoformat()}.md"
    out.write_text(md, encoding="utf-8")
    return out


def send_digest(markdown: str, *, target_date: dt.date | None = None) -> dict:
    """Send the digest via configured webhook (Slack/Discord).

    Uses webhooks.notify, which honors the SLACK_WEBHOOK_URL /
    DISCORD_WEBHOOK_URL env vars. Telegram is not used (it sends only run
    results, through the Telegram channel).

    Returns a dict of {channel: ok} so caller knows what succeeded.
    """
    w = _words()
    target = target_date or (dt.date.today() - dt.timedelta(days=1))
    # webhooks.notify takes a single message; we just send the markdown as text.
    # Slack/Discord will render the first 200 chars as preview. For full
    # readability, we send title + first 500 chars.
    title = w["send_title"].format(date=target.isoformat())
    summary = markdown[:600].replace("# ", "").replace("**", "").replace("\n", " | ")[:500]
    # Send via webhooks.notify (Slack/Discord)
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
    w = _words()
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
        summary_msg = w["activity"].format(date=target.isoformat(), n=channels_ok)
        if ai_result and not ai_result.get("error"):
            summary_msg += w["activity_ai"].format(model=ai_result.get("model", "?"))
        summary_msg += w["activity_end"]
        _db.add_activity("digest_sent", summary_msg)
    except Exception:
        pass
    return {"date": target.isoformat(), "path": str(path), "send": send_result, "ai_summary": ai_result}
