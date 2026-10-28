"""Sentry error tracking indexer — pulls recent issues from Sentry API.

For each registered Sentry project, fetches the last N issues and their event
counts, then upserts into a local table for the dashboard.

Test/live:
- source_type='mock' uses a deterministic generator for local dev.
- For real sources, provide a Sentry auth token with project:read scope.
  Get one at https://sentry.io/settings/account/api/auth-tokens/

CLI:
  python -m ops_dashboard.cli index sentry [--days 7]
"""
from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from .. import db


@dataclass
class SentryIssue:
    project_id: int
    sentry_project_slug: str
    sentry_project_id: str
    project_key: str          # 'initech', 'acme', etc. (mapped)
    issue_id: str
    title: str
    culprit: str
    level: str                # 'error' | 'warning' | 'info' | 'fatal'
    status: str               # 'unresolved' | 'resolved' | 'ignored'
    event_count: int
    user_count: int
    first_seen: int
    last_seen: int
    permalink: str | None
    raw_payload: dict | None = None

    def to_db_dict(self) -> dict:
        d = self.__dict__.copy()
        raw = d.pop("raw_payload")
        d["raw_payload"] = json.dumps(raw, default=str) if raw else None
        return d


class BaseSource(ABC):
    source_type: str = "abstract"

    def __init__(self, project_row: dict, credentials: dict | None):
        self.project_row = project_row
        self.credentials = credentials or {}

    @abstractmethod
    def test(self) -> tuple[bool, str]:
        """Test connection. Returns (ok, message)."""

    @abstractmethod
    def fetch_issues(self, days: int = 7) -> list[SentryIssue]:
        """Fetch recent issues (last N days)."""

    def run(self, days: int = 7) -> dict:
        try:
            issues = self.fetch_issues(days=days)
        except Exception as e:
            db.record_sentry_project_poll(self.project_row["id"], "error", str(e)[:500])
            return {"ok": False, "error": str(e), "fetched": 0, "upserted": 0}
        for issue in issues:
            db.upsert_sentry_issue(**issue.to_db_dict())
        db.record_sentry_project_poll(self.project_row["id"], "ok")
        return {"ok": True, "fetched": len(issues), "upserted": len(issues)}


# ============================================================
# Real Sentry
# ============================================================

class SentrySource(BaseSource):
    source_type = "sentry"

    def __init__(self, project_row: dict, credentials: dict | None):
        super().__init__(project_row, credentials)
        c = credentials or {}
        # Auth token can come from credentials or from the project_row itself
        self.auth_token = (c.get("auth_token") or project_row.get("auth_token") or
                          "").strip()
        # org/project slugs
        self.org_slug = (c.get("org_slug") or project_row.get("org_slug") or "").strip()
        # Numeric project ID (or slug) for the Sentry API
        self.sentry_project = (c.get("sentry_project") or project_row.get("sentry_project") or "").strip()
        self.api_base = "https://sentry.io/api/0"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.auth_token}", "Accept": "application/json"}

    def test(self) -> tuple[bool, str]:
        if not self.auth_token:
            return False, "missing auth_token in credentials or project row"
        if not (self.org_slug and self.sentry_project):
            return False, "missing org_slug and sentry_project"
        try:
            r = httpx.get(
                f"{self.api_base}/projects/{self.org_slug}/{self.sentry_project}/",
                headers=self._headers(), timeout=10,
            )
            if r.status_code == 200:
                p = r.json()
                return True, f"Sentry project '{p.get('slug')}' accessible (id: {p.get('id')})"
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def fetch_issues(self, days: int = 7) -> list[SentryIssue]:
        if not (self.auth_token and self.org_slug and self.sentry_project):
            return []
        # First resolve the Sentry project ID
        r = httpx.get(
            f"{self.api_base}/projects/{self.org_slug}/{self.sentry_project}/",
            headers=self._headers(), timeout=10,
        )
        if r.status_code != 200:
            raise RuntimeError(f"project lookup HTTP {r.status_code}: {r.text[:200]}")
        sentry_project_id = str(r.json().get("id") or "")
        if not sentry_project_id:
            raise RuntimeError("no project id in response")
        # Fetch recent issues
        r2 = httpx.get(
            f"{self.api_base}/projects/{self.org_slug}/{self.sentry_project}/issues/?query=lastSeen%3A-%{days}d&limit=50",
            headers=self._headers(), timeout=15,
        )
        if r2.status_code != 200:
            raise RuntimeError(f"issues HTTP {r2.status_code}: {r2.text[:200]}")
        issues: list[SentryIssue] = []
        for it in r2.json():
            issues.append(SentryIssue(
                project_id=self.project_row["id"],
                sentry_project_slug=self.sentry_project,
                sentry_project_id=sentry_project_id,
                project_key=self.project_row["project_key"],
                issue_id=str(it.get("id") or ""),
                title=(it.get("title") or "")[:500],
                culprit=(it.get("culprit") or "")[:200],
                level=(it.get("level") or "error"),
                status=(it.get("status") or "unresolved"),
                event_count=int(it.get("count") or 0),
                user_count=int(it.get("userCount") or 0),
                first_seen=int(time.mktime(time.strptime(it.get("firstSeen", "").replace("Z", "").split(".")[0], "%Y-%m-%dT%H:%M:%S"))) if it.get("firstSeen") else 0,
                last_seen=int(time.mktime(time.strptime(it.get("lastSeen", "").replace("Z", "").split(".")[0], "%Y-%m-%dT%H:%M:%S"))) if it.get("lastSeen") else 0,
                permalink=it.get("permalink"),
                raw_payload=it,
            ))
        return issues


# ============================================================
# Mock (deterministic generator for local dev)
# ============================================================

class MockSource(BaseSource):
    source_type = "mock"

    def test(self) -> tuple[bool, str]:
        return True, "mock Sentry source (deterministic generator)"

    def fetch_issues(self, days: int = 7) -> list[SentryIssue]:
        seed = int(hashlib.sha1(self.project_row["name"].encode()).hexdigest()[:8], 16)
        rng = seed
        def rand(lo, hi):
            nonlocal rng
            rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
            return lo + (rng % (hi - lo + 1))
        def choice(seq):
            return seq[rand(0, len(seq) - 1)]
        levels = ["error", "error", "warning", "info", "fatal"]
        titles = [
            "TypeError: Cannot read property 'id' of undefined",
            "Unhandled promise rejection: fetch failed",
            "DatabaseError: connection timeout after 30s",
            "ChunkLoadError: Loading chunk 7 failed",
            "RangeError: Maximum call stack size exceeded",
            "Authentication failed: invalid JWT token",
            "Stripe API error: rate_limit_exceeded",
            "CORS policy: No 'Access-Control-Allow-Origin' header",
            "TypeError: items.map is not a function",
            "Network error: ETIMEDOUT",
            "Hydration failed because the initial UI does not match",
            "ResizeObserver loop completed with undelivered notifications",
        ]
        culprits = [
            "src/components/PropertyCard.tsx:42",
            "src/lib/api.ts:115",
            "src/server/routes/properties.ts:88",
            "src/hooks/useAuth.ts:23",
            "src/lib/stripe.ts:67",
            "src/utils/debounce.ts:12",
        ]
        n = rand(2, 8)
        issues = []
        now = int(time.time())
        for i in range(n):
            first_seen = now - rand(0, days * 86400)
            last_seen = first_seen + rand(0, (now - first_seen))
            level = choice(levels)
            event_count = rand(1, 200)
            issues.append(SentryIssue(
                project_id=self.project_row["id"],
                sentry_project_slug=f"mock-{self.project_row['project_key']}",
                sentry_project_id=str(1000 + i),
                project_key=self.project_row["project_key"],
                issue_id=f"mock-{self.project_row['name']}-{i}",
                title=choice(titles),
                culprit=choice(culprits),
                level=level,
                status="unresolved" if rand(0, 4) > 0 else "resolved",
                event_count=event_count,
                user_count=rand(1, min(50, event_count)),
                first_seen=first_seen,
                last_seen=last_seen,
                permalink=f"https://sentry.io/mock/{i}",
                raw_payload={"mock": True, "seed": seed, "i": i},
            ))
        return issues


# ============================================================
# Factory + entry point
# ============================================================

SOURCES: dict[str, type[BaseSource]] = {
    "sentry": SentrySource,
    "mock": MockSource,
}


def get_source(project_row: dict) -> BaseSource | None:
    cls = SOURCES.get(project_row.get("source_type") or "mock")
    if not cls:
        return None
    return cls(project_row, {})


def run(project_id: int | None = None, days: int = 7) -> dict:
    if project_id:
        rows = [db.get_sentry_project(project_id)]
        rows = [r for r in rows if r]
    else:
        rows = db.list_sentry_projects(enabled_only=True)
    if not rows:
        return {"projects": 0, "fetched": 0, "upserted": 0, "results": []}
    results = []
    agg = {"projects": len(rows), "fetched": 0, "upserted": 0, "results": results}
    for row in rows:
        src = get_source(row)
        if not src:
            results.append({"project": row["name"], "ok": False, "error": f"unknown source_type: {row['source_type']}"})
            continue
        res = src.run(days=days)
        res["project"] = row["name"]
        results.append(res)
        agg["fetched"] += res.get("fetched", 0)
        agg["upserted"] += res.get("upserted", 0)
    # Log to activity feed
    try:
        if agg["upserted"] > 0:
            db.add_activity("sentry_poll", f"{agg['upserted']} issues from {agg['projects']} projects")
    except Exception:
        pass
    return agg


def test_project(project_id: int) -> tuple[bool, str]:
    row = db.get_sentry_project(project_id)
    if not row:
        return False, "project not found"
    src = get_source(row)
    if not src:
        return False, f"unknown source_type: {row['source_type']}"
    return src.test()
