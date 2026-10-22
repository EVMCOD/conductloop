"""Google Search Console indexer — pulls daily search metrics via REST.

For each registered site, fetches the last N days of search performance
(clicks, impressions, CTR, average position) and upserts into
``gsc_metrics_daily`` (idempotent on (site_id, date)).

Auth: the site row carries a ``credentials_json`` blob — either an OAuth
authorized_user file (refresh token; what ``~/.ga-keys/token.json`` holds,
needs the ``webmasters`` scope) or a service account key. No extra client
library needed: plain REST via google-auth's AuthorizedSession.

Data notes:
- GSC data lags ~2-3 days; the last day or two of any window will be 0/partial.
- ``site_url`` is exactly what the GSC API lists: ``sc-domain:example.com``
  (domain property) or ``https://example.com/`` (URL-prefix property).

CLI:
  python -m ops_dashboard.cli gsc poll --days 28
  python -m ops_dashboard.cli gsc summary
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse

from .. import db

# Full (not .readonly) — must match the scope the stored refresh token was
# granted with, or the refresh exchange can fail.
SCOPES = ["https://www.googleapis.com/auth/webmasters"]
API_BASE = "https://searchconsole.googleapis.com/webmasters/v3"


def _session(credentials_json: str):
    from google.auth.transport.requests import AuthorizedSession
    info = json.loads(credentials_json)
    # authorized_user files don't always carry a "type" field — detect by
    # refresh_token (same convention as indexers/ga.py).
    if info.get("type") == "authorized_user" or "refresh_token" in info:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_info(info, scopes=SCOPES)
    else:
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(creds)


class GSCSource:
    """One registered Search Console property."""

    def __init__(self, site_row: dict):
        self.site_row = site_row
        self.site_url = site_row["site_url"]
        self.credentials_json = site_row.get("credentials_json") or ""

    def _query(self, body: dict) -> dict:
        sess = _session(self.credentials_json)
        url = f"{API_BASE}/sites/{urllib.parse.quote(self.site_url, safe='')}/searchAnalytics/query"
        resp = sess.post(url, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def test(self) -> tuple[bool, str]:
        if not self.credentials_json:
            return False, "missing credentials_json on site row"
        try:
            today = dt.date.today()
            self._query({
                "startDate": (today - dt.timedelta(days=7)).isoformat(),
                "endDate": today.isoformat(),
                "rowLimit": 1,
            })
            return True, f"GSC property {self.site_url} accessible"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def fetch_metrics(self, days: int = 28) -> list[dict]:
        """One dict per day that has data (GSC omits empty days)."""
        if not self.credentials_json:
            return []
        today = dt.date.today()
        data = self._query({
            "startDate": (today - dt.timedelta(days=days)).isoformat(),
            "endDate": today.isoformat(),
            "dimensions": ["date"],
            "rowLimit": days + 5,
        })
        out: list[dict] = []
        for row in data.get("rows", []):
            out.append({
                "site_id": self.site_row["id"],
                "site_url": self.site_url,
                "project_key": self.site_row["project_key"],
                "date": row["keys"][0],
                "clicks": int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "ctr": round(float(row.get("ctr", 0.0)), 6),
                "position": round(float(row.get("position", 0.0)), 2),
            })
        return out

    def run(self, days: int = 28) -> dict:
        try:
            metrics = self.fetch_metrics(days=days)
        except Exception as e:
            db.record_gsc_site_poll(self.site_row["id"], "error", str(e)[:500])
            return {"ok": False, "error": str(e), "fetched": 0, "upserted": 0}
        upserted = 0
        for m in metrics:
            db.upsert_gsc_metrics_daily(**m)
            upserted += 1
        db.record_gsc_site_poll(self.site_row["id"], "ok")
        return {"ok": True, "fetched": len(metrics), "upserted": upserted}


def poll_all(*, days: int = 28, project_key: str | None = None,
             site_id: int | None = None) -> list[dict]:
    """Poll every enabled site (or one). Returns one result dict per site."""
    rows = db.list_gsc_sites(project_key=project_key, enabled_only=True)
    if site_id is not None:
        rows = [r for r in rows if r["id"] == site_id]
    results: list[dict] = []
    for row in rows:
        res = GSCSource(row).run(days=days)
        res["site"] = row["display_name"]
        results.append(res)
    return results


def test_site(site_id: int) -> tuple[bool, str]:
    row = db.get_gsc_site(site_id)
    if not row:
        return False, f"no gsc site with id {site_id}"
    return GSCSource(row).test()
