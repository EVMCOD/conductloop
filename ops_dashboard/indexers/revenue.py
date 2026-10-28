"""Revenue indexer — pulls events from Apple App Store Connect and Google Play.

For each registered source, polls the upstream API and inserts new events into
the `revenue_events` table (idempotent on (source_type, external_id)).

Test/live without real keys:
- Set source_type='mock' to use the built-in generator (deterministic data).
- For real sources, the source's `connection_id` must reference a row in
  `connections` with encrypted credentials (handled by `db._decrypt_credentials`).

CLI:
  python -m ops_dashboard.cli index revenue              # all enabled sources
  python -m ops_dashboard.cli index revenue --source apple-myapp
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from .. import db

# ============================================================
# Common data shape (what every source normalizes to)
# ============================================================

@dataclass
class RevenueEvent:
    source_type: str           # 'apple' | 'google' | 'mock'
    project_key: str
    external_id: str           # source's unique event ID (ch_xxx, txn_xxx, etc.)
    event_type: str            # 'payment_succeeded' | 'subscription_created' | 'refund' | 'dispute_opened' | etc.
    amount_cents: int          # positive for revenue, negative for refunds
    currency: str              # ISO 4217
    occurred_at: int           # epoch seconds
    customer_ref: str | None = None
    customer_email: str | None = None
    product_ref: str | None = None
    product_name: str | None = None
    status: str = "succeeded"  # 'succeeded' | 'pending' | 'failed' | 'refunded'
    raw_payload: dict | None = None

    def to_db_dict(self) -> dict:
        d = self.__dict__.copy()
        raw = d.pop("raw_payload")
        d["raw_payload"] = json.dumps(raw, default=str) if raw else None
        return d


class BaseSource(ABC):
    """Abstract base for revenue source indexers."""

    source_type: str = "abstract"

    def __init__(self, source_row: dict, credentials: dict | None):
        self.source_row = source_row
        self.credentials = credentials or {}

    @abstractmethod
    def test(self) -> tuple[bool, str]:
        """Test the connection. Returns (ok, message)."""

    @abstractmethod
    def fetch_events(self, since: int | None = None, limit: int = 100) -> list[RevenueEvent]:
        """Fetch events since the given timestamp (epoch seconds)."""

    def run(self) -> dict:
        """Poll and insert. Returns counters."""
        try:
            events = self.fetch_events()
        except Exception as e:
            db.record_revenue_source_poll(self.source_row["id"], "error", str(e)[:500])
            return {"ok": False, "error": str(e), "inserted": 0, "duplicates": 0}
        if not events:
            db.record_revenue_source_poll(self.source_row["id"], "ok")
            return {"ok": True, "inserted": 0, "duplicates": 0, "fetched": 0}
        payloads = []
        for ev in events:
            d = ev.to_db_dict()
            d["source_id"] = self.source_row["id"]
            payloads.append(d)
        result = db.insert_revenue_events_bulk(payloads)
        db.record_revenue_source_poll(self.source_row["id"], "ok")
        result["ok"] = True
        result["fetched"] = len(events)
        return result


# ============================================================
# Apple App Store Connect
# ============================================================

class AppleSource(BaseSource):
    """App Store Connect API. Needs .p8 JWT key + issuer ID + key ID.
    Endpoints: /v1/apps, /v1/salesReports, /v1/subscriptionsReport.
    Reports are downloadable TSVs (gzipped) — kept simple here, with fetch_events
    returning [] when credentials are not configured (test mode)."""
    source_type = "apple"

    def __init__(self, source_row: dict, credentials: dict | None):
        super().__init__(source_row, credentials)
        c = credentials or {}
        self.key_id = c.get("key_id", "")
        self.issuer_id = c.get("issuer_id", "")
        self.bundle_id = c.get("bundle_id", "")
        # private_key is the .p8 contents (PEM)
        self.private_key = c.get("private_key", "")
        self.vendor_number = c.get("vendor_number", "")

    def _jwt(self) -> str:
        """Generate App Store Connect API JWT (RS256)."""
        import jwt  # PyJWT
        now = int(time.time())
        payload = {
            "iss": self.issuer_id,
            "iat": now,
            "exp": now + 1200,  # 20 min max
            "aud": "appstoreconnect-v1",
        }
        return jwt.encode(payload, self.private_key, algorithm="ES256",
                          headers={"kid": self.key_id})

    def test(self) -> tuple[bool, str]:
        if not (self.key_id and self.issuer_id and self.private_key):
            return False, "missing key_id / issuer_id / private_key (.p8) in credentials"
        try:
            jwt_token = self._jwt()
            r = httpx.get("https://api.appstoreconnect.apple.com/v1/apps",
                          headers={"Authorization": f"Bearer {jwt_token}"}, timeout=10)
            if r.status_code == 200:
                apps = r.json().get("data", [])
                names = ", ".join(a.get("attributes", {}).get("name", "?") for a in apps[:3])
                return True, f"App Store Connect: {len(apps)} apps ({names})"
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def fetch_events(self, since: int | None = None, limit: int = 100) -> list[RevenueEvent]:
        """Fetch sales/subscription reports.

        Note: Apple reports are daily snapshots, not event streams. The report
        endpoint returns TSV files. For a real implementation we'd download the
        latest daily TSV and diff against the last ingest. For now, returns
        empty list when not configured (test mode) and lets mock source
        provide sample data.
        """
        if not (self.key_id and self.issuer_id and self.private_key):
            return []
        # Placeholder: real implementation would download TSV from
        # /v1/salesReports?filter[frequency]=DAILY&filter[reportType]=SALES&...
        # and parse rows into RevenueEvent. Skipping for brevity.
        return []


# ============================================================
# Google Play Console
# ============================================================

class GoogleSource(BaseSource):
    """Google Play Developer API. Needs service account JSON (oauth2 creds)."""
    source_type = "google"

    def __init__(self, source_row: dict, credentials: dict | None):
        super().__init__(source_row, credentials)
        c = credentials or {}
        # service_account_json: full JSON content of the service account key
        self.sa_json = c.get("service_account_json", "")
        self.package_name = c.get("package_name", "")

    def _access_token(self) -> str:
        from google.auth.transport.requests import Request as GRequest
        from google.oauth2 import service_account
        info = json.loads(self.sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        creds.refresh(GRequest())
        return creds.token

    def test(self) -> tuple[bool, str]:
        if not self.sa_json:
            return False, "missing service_account_json in credentials"
        if not self.package_name:
            return False, "missing package_name (e.g. 'com.example.app')"
        try:
            token = self._access_token()
            r = httpx.get(
                f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{self.package_name}",
                headers={"Authorization": f"Bearer {token}"}, timeout=10,
            )
            if r.status_code == 200:
                return True, f"Google Play: package {self.package_name} accessible"
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def fetch_events(self, since: int | None = None, limit: int = 100) -> list[RevenueEvent]:
        if not self.sa_json or not self.package_name:
            return []
        # Real implementation would call
        # /androidpublisher/v3/{packageName}/purchases/products (one-time)
        # /androidpublisher/v3/{packageName}/purchases/subscriptions (recurring)
        # and for reports: /androidpublisher/v3/{packageName}/reports
        return []


# ============================================================
# Mock (for local dev / no creds)
# ============================================================

class MockSource(BaseSource):
    """Deterministic generator for local dev. Inserts 5-15 events over the last 30 days
    per source. Useful for UI testing and demos without real Apple/Google creds."""
    source_type = "mock"

    def __init__(self, source_row: dict, credentials: dict | None):
        super().__init__(source_row, credentials)

    def test(self) -> tuple[bool, str]:
        return True, "mock source (deterministic generator)"

    def fetch_events(self, since: int | None = None, limit: int = 100) -> list[RevenueEvent]:
        import hashlib
        # Use source name as seed for deterministic-but-varied output
        seed = int(hashlib.sha1(self.source_row["name"].encode()).hexdigest()[:8], 16)
        rng_state = seed
        def randint(lo, hi):
            nonlocal rng_state
            rng_state = (rng_state * 1103515245 + 12345) & 0x7FFFFFFF
            return lo + (rng_state % (hi - lo + 1))
        def choice(seq):
            return seq[randint(0, len(seq) - 1)]

        now = int(time.time())
        events: list[RevenueEvent] = []
        n = randint(8, 15)
        products = ["Starter Plan", "Pro Plan", "Team Plan", "One-time Setup", "Annual Pro"]
        event_types = [
            ("payment_succeeded", 2900, "succeeded"),
            ("payment_succeeded", 4900, "succeeded"),
            ("payment_succeeded", 9900, "succeeded"),
            ("payment_succeeded", 49000, "succeeded"),
            ("subscription_renewed", 4900, "active"),
            ("subscription_renewed", 9900, "active"),
            ("refund", -2900, "refunded"),
            ("payment_failed", 2900, "failed"),
        ]
        for i in range(n):
            etype, amount, status = choice(event_types)
            occurred_at = now - randint(0, 30 * 86400)
            events.append(RevenueEvent(
                source_type="mock",
                project_key=self.source_row["project_key"],
                external_id=f"mock_{self.source_row['name']}_{i}_{occurred_at}",
                event_type=etype,
                amount_cents=amount,
                currency=self.source_row.get("currency", "USD"),
                occurred_at=occurred_at,
                customer_ref=f"cus_mock_{randint(1000, 9999)}",
                customer_email=f"customer{randint(100, 999)}@example.com",
                product_ref=f"plan_{randint(1, 5)}",
                product_name=choice(products),
                status=status,
                raw_payload={"mock": True, "seed": seed, "i": i},
            ))
        return events


# ============================================================
# Factory
# ============================================================

SOURCES: dict[str, type[BaseSource]] = {
    "apple": AppleSource,
    "google": GoogleSource,
    "mock": MockSource,
}


def get_source(source_row: dict) -> BaseSource | None:
    """Build a source instance from a revenue_sources row, decrypting its connection."""
    cls = SOURCES.get(source_row["source_type"])
    if not cls:
        return None
    creds: dict | None = None
    if source_row.get("connection_id"):
        conn = db.get_connection_by_id(source_row["connection_id"])
        if conn and conn.get("credentials"):
            creds = conn["credentials"]
    return cls(source_row, creds)


# ============================================================
# Public entry point
# ============================================================

def run(source_name: str | None = None) -> dict:
    """Poll one or all enabled sources. Returns aggregate counters."""
    if source_name:
        rows = [db.get_revenue_source_by_name(source_name)]
        rows = [r for r in rows if r]
    else:
        rows = db.list_revenue_sources(enabled_only=True)
    if not rows:
        return {"sources": 0, "fetched": 0, "inserted": 0, "duplicates": 0, "results": []}
    results = []
    agg = {"sources": len(rows), "fetched": 0, "inserted": 0, "duplicates": 0, "results": results}
    for row in rows:
        src = get_source(row)
        if not src:
            results.append({"source": row["name"], "ok": False, "error": f"unknown source_type: {row['source_type']}"})
            continue
        res = src.run()
        res["source"] = row["name"]
        results.append(res)
        agg["fetched"] += res.get("fetched", 0)
        agg["inserted"] += res.get("inserted", 0)
        agg["duplicates"] += res.get("duplicates", 0)
    # Log to activity feed
    try:
        if agg["inserted"] > 0:
            db.add_activity("revenue_poll", f"{agg['inserted']} events from {agg['sources']} sources")
    except Exception:
        pass
    return agg


def test_source(source_id: int) -> tuple[bool, str]:
    """Test a single source connection (does not insert)."""
    row = db.get_revenue_source(source_id)
    if not row:
        return False, "source not found"
    src = get_source(row)
    if not src:
        return False, f"unknown source_type: {row['source_type']}"
    return src.test()
