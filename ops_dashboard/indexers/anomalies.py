"""Anomaly detection — z-score and pct-change vs rolling baseline.

For each daily metric, compare the last 24h to the prior 7-day mean+std. Flag
events that are |z| > 2 (about 5% probability under normal distribution) or
absolute change > 50%.

Currently monitors:
- Revenue (per project + total)
- GA4 users (per site + total)
- Live URL health (status flips)

Output: list of {kind, severity, project, message, current, baseline, zscore}
"""
from __future__ import annotations

import datetime as dt
import statistics
import time

from .. import db

# The same alert is written to the activity feed at most once per this window
# (detection runs on every dashboard load, digest and briefing).
FEED_DEDUPE_SECONDS = 24 * 3600

# The words of the anomaly messages, in the language the person chose. The
# module that owns the words owns the dictionary.
#
# The messages used to be built with the internal kind ("revenue_total",
# "ga_users_acme") and English words around it, and the panel shows them
# as they are: the person read "revenue_total ↓ -71% vs 7d baseline (12 vs
# avg 43)" in an app that had just promised to speak their language.
_WORDS: dict[str, dict[str, str]] = {
    "en": {
        "revenue_total": "Total revenue",
        "ga_users_total": "Total GA users",
        "revenue_project": "{project} revenue",
        "ga_users_project": "{project} GA users",
        "vs_baseline": "{pct} vs 7d baseline ({current} vs avg {base})",
        "revenue_check_failed": "revenue check failed: {err}",
        "ga_check_failed": "GA check failed: {err}",
        "url_check_failed": "URL check failed: {err}",
        "url_unhealthy": "URL {name} unhealthy: {status}",
        "http": "HTTP {code}",
        "no_response": "no response",
    },
    "es": {
        "revenue_total": "Ingresos totales",
        "ga_users_total": "Usuarios totales de GA",
        "revenue_project": "Ingresos de {project}",
        "ga_users_project": "Usuarios de GA de {project}",
        "vs_baseline": "{pct} frente a la media de 7 días ({current} vs {base})",
        "revenue_check_failed": "no se pudieron comprobar los ingresos: {err}",
        "ga_check_failed": "no se pudo comprobar GA: {err}",
        "url_check_failed": "no se pudieron comprobar las URLs: {err}",
        "url_unhealthy": "URL {name} no responde: {status}",
        "http": "HTTP {code}",
        "no_response": "sin respuesta",
    },
}


def _words() -> dict[str, str]:
    """The words of the anomaly messages, in the language of the person."""
    from .. import i18n

    try:
        return _WORDS[i18n.current_locale()]
    except Exception:
        return _WORDS["en"]


def _metric_label(metric: str, project_name: str | None) -> str:
    """What the anomaly is about, in words a person would use."""
    w = _words()
    if project_name:
        key = "revenue_project" if metric == "revenue" else "ga_users_project"
        return w[key].format(project=project_name)
    key = "revenue_total" if metric == "revenue" else "ga_users_total"
    return w[key]


def _label_for_kind(kind: str) -> str:
    """The human label of an anomaly, when only its kind is known."""
    metric = "ga_users" if kind.startswith("ga_users") else "revenue"
    if kind.endswith("_total"):
        return _metric_label(metric, None)
    try:
        from .. import projects as proj_mod
        for p in proj_mod.PROJECTS:
            if kind == f"{metric}_{p.key}":
                return _metric_label(metric, p.name)
    except Exception:
        pass
    return _metric_label(metric, None)


def _rolling_stats(values: list[float], window: int = 7) -> tuple[float, float]:
    """Mean and stdev of the last `window` non-zero values. Returns (0, 1) if empty."""
    nonzero = [v for v in values[-window:] if v is not None]
    if not nonzero:
        return 0.0, 1.0
    if len(nonzero) < 2:
        return nonzero[0], max(nonzero[0] * 0.1, 1.0)
    return statistics.mean(nonzero), max(statistics.stdev(nonzero), 1.0)


def _complete_days(series: list[dict]) -> list[dict]:
    """Drop trailing points for a day that has not ended yet.

    Today's GA users or revenue are a fraction of a full day, so comparing them
    with full days flagged a big "drop" every morning. Revenue dates are UTC and
    GA dates are local, so a day counts as finished only once it has ended in both.
    """
    first_open = min(dt.date.today(), dt.datetime.now(dt.UTC).date()).isoformat()
    end = len(series)
    while end and str(series[end - 1].get("date") or "") >= first_open:
        end -= 1
    return series[:end]


def _check_series(series: list[dict], value_key: str, kind: str, project: str,
                  label: str | None = None) -> list[dict]:
    """Given a list of {date, x, y} points, check the latest complete day vs baseline."""
    series = _complete_days(series)
    if len(series) < 3:
        return []
    current = series[-1].get(value_key, 0) or 0
    prior = [s.get(value_key, 0) or 0 for s in series[:-1]]
    base, std = _rolling_stats(prior, window=7)
    if std == 0:
        std = max(abs(base) * 0.1, 1.0)
    z = (current - base) / std
    pct = ((current - base) / base * 100) if base > 0 else 0
    anomalies = []
    if abs(z) >= 2.0 and base > 0:
        sev = "high" if abs(z) >= 3.0 else "medium"
        detail = _words()["vs_baseline"].format(
            pct=f"{pct:+.0f}%", current=int(current), base=int(base))
        anomalies.append({
            "kind": kind, "project": project,
            "date": series[-1].get("date"),
            "current": current, "baseline": round(base, 1), "stddev": round(std, 1),
            "zscore": round(z, 2), "pct_change": round(pct, 1),
            "severity": sev,
            "direction": "spike" if z > 0 else "drop",
            "label": label or _label_for_kind(kind),
            "message": f"{label or _label_for_kind(kind)} {'↑' if z > 0 else '↓'} {detail}",
        })
    return anomalies


def _log_to_feed(anomalies) -> None:
    """Add each alert to the activity feed unless the same one is already there
    from the last FEED_DEDUPE_SECONDS (it was logged on every page load)."""
    since = int(time.time()) - FEED_DEDUPE_SECONDS
    for a in anomalies:
        summary = a.get("message") or a.get("kind", "?")
        with db.db() as conn:
            seen = conn.execute(
                "SELECT 1 FROM activity_feed WHERE kind = 'anomaly_detected' AND summary = ? AND created_at >= ? LIMIT 1",
                (summary, since),
            ).fetchone()
        if not seen:
            db.add_activity("anomaly_detected", summary, project_key=a.get("project"))


def detect_recent_anomalies() -> dict:
    """Check the most recent day of revenue + GA + URLs for anomalies."""
    anomalies: list[dict] = []

    # 1) Revenue per project + total
    try:
        # Use the forecast function's data path
        forecast = db.revenue_forecast(days=1, project_key=None)
        # Build series from history
        history = forecast.get("history", [])
        total_series = history  # already aggregated across all projects
        anomalies.extend(_check_series(total_series, "y", "revenue_total", "all",
                                       _metric_label("revenue", None)))

        # Per-project: we'd need to call forecast with project_key for each
        from .. import projects as proj_mod
        for p in proj_mod.PROJECTS:
            pf = db.revenue_forecast(days=1, project_key=p.key)
            ph = pf.get("history", [])
            anomalies.extend(_check_series(ph, "y", f"revenue_{p.key}", p.key,
                                           _metric_label("revenue", p.name)))
    except Exception as e:
        anomalies.append({"kind": "revenue_check", "severity": "info",
                          "message": _words()["revenue_check_failed"].format(err=e)})

    # 2) GA per site + total
    try:
        from .. import projects as proj_mod
        for p in proj_mod.PROJECTS:
            gf = db.ga_forecast(days=1, metric="users", project_key=p.key)
            gh = gf.get("history", [])
            anomalies.extend(_check_series(gh, "y", f"ga_users_{p.key}", p.key,
                                           _metric_label("ga_users", p.name)))
        gf_total = db.ga_forecast(days=1, metric="users", project_key=None)
        anomalies.extend(_check_series(gf_total.get("history", []), "y", "ga_users_total", "all",
                                       _metric_label("ga_users", None)))
    except Exception as e:
        anomalies.append({"kind": "ga_check", "severity": "info",
                          "message": _words()["ga_check_failed"].format(err=e)})

    # 3) Live URL health — check for recent status flips
    try:
        from . import live_urls
        # The live_urls table has recent checks; look for status_code=0 or != 200 in last 24h
        rows = live_urls.recent_unhealthy(hours=24)
        w = _words()
        for r in rows:
            status = (w["http"].format(code=r.get("status_code"))
                      if r.get("status_code") else w["no_response"])
            anomalies.append({
                "kind": "url_health", "project": r.get("project") or "—",
                "severity": "high" if r.get("status_code") in (0, None) else "medium",
                "name": r.get("name"),
                "url": r.get("url"),
                "status_code": r.get("status_code"),
                "message": w["url_unhealthy"].format(name=r.get("name"), status=status),
            })
    except Exception as e:
        anomalies.append({"kind": "url_check", "severity": "info",
                          "message": _words()["url_check_failed"].format(err=e)})

    # Sort by severity (high first)
    sev_order = {"high": 0, "medium": 1, "info": 2, "low": 3}
    anomalies.sort(key=lambda a: sev_order.get(a.get("severity", "info"), 9))
    # Log high/medium to activity feed (skip info)
    try:
        _log_to_feed(a for a in anomalies if a.get("severity") in ("high", "medium"))
    except Exception:
        pass
    return {
        "checked_at": int(time.time()),
        "anomalies": anomalies,
        "count": len(anomalies),
        "by_severity": {
            "high": sum(1 for a in anomalies if a.get("severity") == "high"),
            "medium": sum(1 for a in anomalies if a.get("severity") == "medium"),
            "info": sum(1 for a in anomalies if a.get("severity") == "info"),
        }
    }
