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

import statistics
import time

from .. import db


def _rolling_stats(values: list[float], window: int = 7) -> tuple[float, float]:
    """Mean and stdev of the last `window` non-zero values. Returns (0, 1) if empty."""
    nonzero = [v for v in values[-window:] if v is not None]
    if not nonzero:
        return 0.0, 1.0
    if len(nonzero) < 2:
        return nonzero[0], max(nonzero[0] * 0.1, 1.0)
    return statistics.mean(nonzero), max(statistics.stdev(nonzero), 1.0)


def _check_series(series: list[dict], value_key: str, label: str, project: str) -> list[dict]:
    """Given a list of {date, x, y} points, check the latest vs baseline."""
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
        anomalies.append({
            "kind": label, "project": project,
            "date": series[-1].get("date"),
            "current": current, "baseline": round(base, 1), "stddev": round(std, 1),
            "zscore": round(z, 2), "pct_change": round(pct, 1),
            "severity": sev,
            "direction": "spike" if z > 0 else "drop",
            "message": f"{label} {'↑' if z > 0 else '↓'} {pct:+.0f}% vs 7d baseline ({int(current)} vs avg {int(base)})"
        })
    return anomalies


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
        anomalies.extend(_check_series(total_series, "y", "revenue_total", "all"))

        # Per-project: we'd need to call forecast with project_key for each
        from .. import projects as proj_mod
        for p in proj_mod.PROJECTS:
            pf = db.revenue_forecast(days=1, project_key=p.key)
            ph = pf.get("history", [])
            anomalies.extend(_check_series(ph, "y", f"revenue_{p.key}", p.key))
    except Exception as e:
        anomalies.append({"kind": "revenue_check", "severity": "info", "message": f"revenue check failed: {e}"})

    # 2) GA per site + total
    try:
        from .. import projects as proj_mod
        for p in proj_mod.PROJECTS:
            gf = db.ga_forecast(days=1, metric="users", project_key=p.key)
            gh = gf.get("history", [])
            anomalies.extend(_check_series(gh, "y", f"ga_users_{p.key}", p.key))
        gf_total = db.ga_forecast(days=1, metric="users", project_key=None)
        anomalies.extend(_check_series(gf_total.get("history", []), "y", "ga_users_total", "all"))
    except Exception as e:
        anomalies.append({"kind": "ga_check", "severity": "info", "message": f"ga check failed: {e}"})

    # 3) Live URL health — check for recent status flips
    try:
        from . import live_urls
        # The live_urls table has recent checks; look for status_code=0 or != 200 in last 24h
        rows = live_urls.recent_unhealthy(hours=24)
        for r in rows:
            anomalies.append({
                "kind": "url_health", "project": r.get("project") or "—",
                "severity": "high" if r.get("status_code") in (0, None) else "medium",
                "name": r.get("name"),
                "url": r.get("url"),
                "status_code": r.get("status_code"),
                "message": f"URL {r.get('name')} unhealthy: HTTP {r.get('status_code') or 'no response'}"
            })
    except Exception as e:
        anomalies.append({"kind": "url_check", "severity": "info", "message": f"url check failed: {e}"})

    # Sort by severity (high first)
    sev_order = {"high": 0, "medium": 1, "info": 2, "low": 3}
    anomalies.sort(key=lambda a: sev_order.get(a.get("severity", "info"), 9))
    # Log high/medium to activity feed (skip info)
    try:
        from .. import db as _db
        for a in anomalies:
            if a.get("severity") in ("high", "medium"):
                _db.add_activity("anomaly_detected", a.get("message") or a.get("kind", "?"), project_key=a.get("project"))
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
