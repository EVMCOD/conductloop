"""Google Analytics 4 indexer — pulls daily metrics from GA4 Data API.

For each registered site, fetches the last N days of traffic (users, sessions,
page_views, engagement, traffic sources) and upserts into `ga_metrics_daily`
(idempotent on (site_id, date)).

Test/live:
- source_type='mock' uses a deterministic generator for local dev.
- For real sources, provide a service account JSON with access to the GA4 property.
  The service account email must be added as a Viewer in GA4 Admin > Property Access.

CLI:
  python -m ops_dashboard.cli index ga              # all enabled sites (last 7 days)
  python -m ops_dashboard.cli index ga --days 30    # last 30 days
  python -m ops_dashboard.cli index ga --site 3     # one site by id
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .. import db


@dataclass
class GAMetricsDay:
    """One day of metrics for one site."""
    site_id: int
    property_id: str
    project_key: str
    date: str                            # YYYY-MM-DD
    users: int = 0
    new_users: int = 0
    sessions: int = 0
    page_views: int = 0
    events: int = 0
    engagement_rate: float | None = None
    avg_session_duration: float | None = None
    bounce_rate: float | None = None
    source_organic: int = 0
    source_direct: int = 0
    source_referral: int = 0
    source_social: int = 0
    source_email: int = 0
    source_paid: int = 0
    top_country: str | None = None
    raw_payload: dict | None = None

    def to_db_dict(self) -> dict:
        # Drop private accumulator attrs (_er_sum etc.) that fetch_metrics
        # parks on the instance while aggregating rows.
        d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        raw = d.pop("raw_payload")
        d["raw_payload"] = json.dumps(raw, default=str) if raw else None
        return d


class BaseSource(ABC):
    source_type: str = "abstract"

    def __init__(self, site_row: dict, credentials: dict | None):
        self.site_row = site_row
        self.credentials = credentials or {}

    @abstractmethod
    def test(self) -> tuple[bool, str]:
        """Test the connection. Returns (ok, message)."""

    @abstractmethod
    def fetch_metrics(self, days: int = 7) -> list[GAMetricsDay]:
        """Fetch the last N days of metrics."""

    def run(self, days: int = 7) -> dict:
        try:
            metrics = self.fetch_metrics(days=days)
        except Exception as e:
            db.record_ga_site_poll(self.site_row["id"], "error", str(e)[:500])
            return {"ok": False, "error": str(e), "fetched": 0, "upserted": 0}
        upserted = 0
        for m in metrics:
            db.upsert_ga_metrics_daily(**m.to_db_dict())
            upserted += 1
        db.record_ga_site_poll(self.site_row["id"], "ok")
        return {"ok": True, "fetched": len(metrics), "upserted": upserted}


# ============================================================
# Google Analytics 4 (real)
# ============================================================

class GA4Source(BaseSource):
    source_type = "ga4"

    def __init__(self, site_row: dict, credentials: dict | None):
        super().__init__(site_row, credentials)
        # property_id is stored as either '123456789' or 'properties/123456789'
        pid = (site_row.get("property_id") or "").strip()
        if pid and not pid.startswith("properties/"):
            pid = f"properties/{pid}"
        self.property_id = pid
        # service account JSON is stored on the site_row itself
        self.sa_json = (site_row.get("service_account_json") or "") or (credentials or {}).get("service_account_json", "")

    def _client(self):
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        info = json.loads(self.sa_json)
        scopes = ["https://www.googleapis.com/auth/analytics.readonly"]
        # The credential JSON can be either a service account key or an OAuth
        # authorized_user blob — both are accepted here. authorized_user files
        # don't always carry a "type" field, so detect by refresh_token.
        if info.get("type") == "authorized_user" or "refresh_token" in info:
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_info(info, scopes=scopes)
        else:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        return BetaAnalyticsDataClient(credentials=creds)

    def test(self) -> tuple[bool, str]:
        if not self.sa_json:
            return False, "missing service_account_json on site row or in connection credentials"
        try:
            client = self._client()
            # Check that the property is accessible via a metadata call.
            from google.analytics.data_v1beta.types import GetMetadataRequest
            req = GetMetadataRequest(name=f"{self.property_id}/metadata")
            md = client.get_metadata(request=req)
            return True, f"GA4 property {self.property_id} accessible ({len(md.dimensions)} dimensions, {len(md.metrics)} metrics)"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def fetch_metrics(self, days: int = 7) -> list[GAMetricsDay]:
        from google.analytics.data_v1beta.types import (
            DateRange,
            Dimension,
            Metric,
            RunReportRequest,
        )
        if not self.sa_json:
            return []
        client = self._client()
        # We pull a single 'today' report with session-scoped traffic source breakdown
        # to keep the request count low. For multi-day, we use date as dimension.
        request = RunReportRequest(
            property=self.property_id,
            dimensions=[
                Dimension(name="date"),
                Dimension(name="sessionDefaultChannelGroup"),  # organic/direct/referral/social/email/paid
                Dimension(name="country"),                       # for top_country
            ],
            metrics=[
                Metric(name="totalUsers"),
                Metric(name="newUsers"),
                Metric(name="sessions"),
                Metric(name="screenPageViews"),
                Metric(name="eventCount"),
                Metric(name="engagementRate"),
                Metric(name="averageSessionDuration"),
                Metric(name="bounceRate"),
            ],
            date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
            limit=100000,
        )
        response = client.run_report(request)
        # Aggregate per (date) so we get one row per day per site.
        per_day: dict[str, GAMetricsDay] = {}
        # Per-date channel sums and per-date country sums for top_country
        for row in response.rows:
            date_str = self._parse_ga_date(row.dimension_values[0].value)
            channel = row.dimension_values[1].value
            country = row.dimension_values[2].value
            users = int(row.metric_values[0].value or 0)
            new_users = int(row.metric_values[1].value or 0)
            sessions = int(row.metric_values[2].value or 0)
            page_views = int(row.metric_values[3].value or 0)
            events = int(row.metric_values[4].value or 0)
            engagement_rate = float(row.metric_values[5].value or 0)
            avg_dur = float(row.metric_values[6].value or 0)
            bounce = float(row.metric_values[7].value or 0)
            d = per_day.setdefault(date_str, GAMetricsDay(
                site_id=self.site_row["id"],
                property_id=self.property_id,
                project_key=self.site_row["project_key"],
                date=date_str,
            ))
            d.users += users
            d.new_users += new_users
            d.sessions += sessions
            d.page_views += page_views
            d.events += events
            # Engagement metrics: average over rows weighted by sessions
            d._er_sum = getattr(d, "_er_sum", 0.0) + engagement_rate * sessions
            d._er_w = getattr(d, "_er_w", 0) + sessions
            d._dur_sum = getattr(d, "_dur_sum", 0.0) + avg_dur * sessions
            d._bounce_sum = getattr(d, "_bounce_sum", 0.0) + bounce * sessions
            # Channel sums
            ch_lower = channel.lower()
            if "organic" in ch_lower: d.source_organic += users
            elif "direct" in ch_lower: d.source_direct += users
            elif "referral" in ch_lower: d.source_referral += users
            elif "social" in ch_lower: d.source_social += users
            elif "email" in ch_lower: d.source_email += users
            elif "paid" in ch_lower: d.source_paid += users
            # Country tracking
            country_counts = getattr(d, "_country_counts", {})
            country_counts[country] = country_counts.get(country, 0) + users
            d._country_counts = country_counts

        results: list[GAMetricsDay] = []
        for d in per_day.values():
            if d._er_w > 0:
                d.engagement_rate = round(d._er_sum / d._er_w, 4)
                d.avg_session_duration = round(d._dur_sum / d._er_w, 2)
                d.bounce_rate = round(d._bounce_sum / d._er_w, 4)
            cc = getattr(d, "_country_counts", {})
            if cc:
                d.top_country = max(cc, key=cc.get)
            d.raw_payload = {"property": self.property_id, "date": d.date}
            results.append(d)
        return results

    @staticmethod
    def _parse_ga_date(ga_date: str) -> str:
        """GA4 date is YYYYMMDD, return YYYY-MM-DD."""
        if len(ga_date) == 8 and ga_date.isdigit():
            return f"{ga_date[:4]}-{ga_date[4:6]}-{ga_date[6:8]}"
        return ga_date


# ============================================================
# Mock (deterministic generator for local dev)
# ============================================================

class MockSource(BaseSource):
    source_type = "mock"

    def test(self) -> tuple[bool, str]:
        return True, "mock GA source (deterministic generator)"

    def fetch_metrics(self, days: int = 7) -> list[GAMetricsDay]:
        seed = int(hashlib.sha1(self.site_row["property_id"].encode()).hexdigest()[:8], 16)
        rng = seed
        def rand(lo, hi):
            nonlocal rng
            rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
            return lo + (rng % (hi - lo + 1))
        def choice(seq):
            return seq[rand(0, len(seq) - 1)]
        # Different sites have different traffic levels (seeded)
        base_users = rand(50, 2000)
        countries = ["ES", "US", "DE", "FR", "MX", "AR", "CO", "GB", "IT", "BR", "CL", "PE"]
        results: list[GAMetricsDay] = []
        today = dt.date.today()
        for i in range(days):
            date = today - dt.timedelta(days=days - 1 - i)
            date_str = date.isoformat()
            # Daily variation: weekday vs weekend + noise
            is_weekend = date.weekday() >= 5
            weekday_mult = 0.6 if is_weekend else 1.0
            noise = 0.7 + (rand(0, 60) / 100.0)  # 0.7-1.3
            users = int(base_users * weekday_mult * noise * (1 + i * 0.02))  # slight growth
            new_users = int(users * (0.3 + rand(0, 50) / 100.0))
            sessions = int(users * (1.1 + rand(0, 30) / 100.0))
            page_views = int(sessions * (1.5 + rand(0, 200) / 100.0))
            events = int(page_views * (0.3 + rand(0, 50) / 100.0))
            # Channel distribution
            org = int(users * 0.45)
            direct = int(users * 0.25)
            ref = int(users * 0.12)
            soc = int(users * 0.10)
            em = int(users * 0.05)
            paid = int(users * 0.03)
            # Normalize so they sum to users
            total = org + direct + ref + soc + em + paid
            if total > 0 and total != users:
                scale = users / total
                org, direct, ref, soc, em, paid = (int(x * scale) for x in (org, direct, ref, soc, em, paid))
            d = GAMetricsDay(
                site_id=self.site_row["id"],
                property_id=self.site_row["property_id"],
                project_key=self.site_row["project_key"],
                date=date_str,
                users=users, new_users=new_users, sessions=sessions,
                page_views=page_views, events=events,
                engagement_rate=round(0.45 + rand(0, 40) / 100.0, 3),
                avg_session_duration=round(60 + rand(0, 180), 1),
                bounce_rate=round(0.35 + rand(0, 30) / 100.0, 3),
                source_organic=org, source_direct=direct, source_referral=ref,
                source_social=soc, source_email=em, source_paid=paid,
                top_country=choice(countries),
                raw_payload={"mock": True, "seed": seed, "i": i},
            )
            results.append(d)
        return results


# ============================================================
# Factory + entry point
# ============================================================

SOURCES: dict[str, type[BaseSource]] = {
    "ga4": GA4Source,
    "mock": MockSource,
}


def get_source(site_row: dict) -> BaseSource | None:
    # No explicit source_type: a site with a service account is real GA4,
    # anything else is the mock generator.
    default = "ga4" if (site_row.get("service_account_json") or "").strip() else "mock"
    cls = SOURCES.get(site_row.get("source_type") or default)
    if not cls:
        return None
    # Credentials (currently service_account_json is on the site row itself,
    # not a separate connection). Pass empty for now.
    return cls(site_row, {})


def run(site_id: int | None = None, days: int = 7) -> dict:
    """Poll one or all enabled sites. Returns aggregate counters."""
    if site_id:
        rows = [db.get_ga_site(site_id)]
        rows = [r for r in rows if r]
    else:
        rows = db.list_ga_sites(enabled_only=True)
    if not rows:
        return {"sites": 0, "fetched": 0, "upserted": 0, "results": []}
    results = []
    agg = {"sites": len(rows), "fetched": 0, "upserted": 0, "results": results}
    for row in rows:
        src = get_source(row)
        if not src:
            results.append({"site": row["display_name"], "ok": False, "error": f"unknown source_type: {row['source_type']}"})
            continue
        res = src.run(days=days)
        res["site"] = row["display_name"]
        results.append(res)
        agg["fetched"] += res.get("fetched", 0)
        agg["upserted"] += res.get("upserted", 0)
    # Log to activity feed
    try:
        if agg["upserted"] > 0:
            db.add_activity("ga_poll", f"{agg['upserted']} metric-days from {agg['sites']} sites")
    except Exception:
        pass
    return agg


def test_site(site_id: int) -> tuple[bool, str]:
    row = db.get_ga_site(site_id)
    if not row:
        return False, "site not found"
    src = get_source(row)
    if not src:
        return False, f"unknown source_type: {row['source_type']}"
    return src.test()
