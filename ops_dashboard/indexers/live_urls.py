"""Live URL health checker.

Pings a known list of URLs (workers.dev, custom domains, Pages deployments),
records status code + latency + a small response body fingerprint.

We keep the most recent health-check rows in a dedicated table so the
dashboard can render status without re-hitting the network every page load.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from pathlib import Path

import httpx

from .. import db

# Identity-neutral by default: the market build ships with NO pre-seeded URLs.
# The set monitored comes from (in order): an optional JSON seed file pointed
# at by OPS_LIVE_URLS_FILE, then whatever the user has already added (stored in
# the live_urls table via the UI). See _configured_urls().
KNOWN_URLS: list[dict] = []


def _configured_urls() -> list[dict]:
    """Return the URLs to monitor. Empty by default.

    1. OPS_LIVE_URLS_FILE — path to a JSON list of {name,url,project,kind}.
    2. Rows already present in the live_urls table (user-added via the UI).
    """
    if KNOWN_URLS:
        return KNOWN_URLS
    path = os.environ.get("OPS_LIVE_URLS_FILE")
    if path:
        try:
            with open(Path(path).expanduser()) as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
    try:
        with db.db() as conn:
            rows = conn.execute(
                "SELECT name, url, project, kind FROM live_urls GROUP BY name"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_urls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,
    url          TEXT    NOT NULL,
    project      TEXT,
    kind         TEXT,
    checked_at   INTEGER NOT NULL,
    status_code  INTEGER,
    ok           INTEGER NOT NULL,           -- 1 = 2xx, 0 = error
    latency_ms   INTEGER,
    error        TEXT,
    final_url    TEXT                        -- after redirects
);

CREATE INDEX IF NOT EXISTS idx_live_urls_checked ON live_urls(checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_live_urls_project ON live_urls(project);

-- The history of the checks. The table above keeps the *current* state: one
-- row per URL, and every check replaces the previous one (the name is
-- unique). That is fine for "is it up right now" and useless for "when did
-- it go down", because the previous check is gone by the time you ask. This
-- table appends one row per check, so the flips can be read back.
CREATE TABLE IF NOT EXISTS live_url_checks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    url          TEXT    NOT NULL,
    project      TEXT,
    kind         TEXT,
    checked_at   INTEGER NOT NULL,
    status_code  INTEGER,
    ok           INTEGER NOT NULL,
    latency_ms   INTEGER,
    error        TEXT,
    final_url    TEXT
);

CREATE INDEX IF NOT EXISTS idx_live_url_checks_name ON live_url_checks(name, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_live_url_checks_checked ON live_url_checks(checked_at DESC);
"""


def ensure_schema() -> None:
    with db.db() as conn:
        conn.executescript(EXTRA_SCHEMA)


def _check(client: httpx.Client, entry: dict, timeout: float = 10.0) -> dict:
    t0 = time.perf_counter()
    try:
        r = client.get(entry["url"], timeout=timeout, follow_redirects=True)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "status_code": r.status_code,
            "ok": 1 if 200 <= r.status_code < 400 else 0,
            "latency_ms": latency_ms,
            "error": None,
            "final_url": str(r.url),
        }
    except Exception as e:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "status_code": None,
            "ok": 0,
            "latency_ms": latency_ms,
            "error": f"{type(e).__name__}: {e}",
            "final_url": entry["url"],
        }


def _record(entry: dict, result: dict) -> None:
    with db.db() as conn:
        conn.execute(
            """
            INSERT INTO live_urls (name, url, project, kind, checked_at, status_code, ok, latency_ms, error, final_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                url         = excluded.url,
                project     = excluded.project,
                kind        = excluded.kind,
                checked_at  = excluded.checked_at,
                status_code = excluded.status_code,
                ok          = excluded.ok,
                latency_ms  = excluded.latency_ms,
                error       = excluded.error,
                final_url   = excluded.final_url
            """,
            (
                entry["name"],
                entry["url"],
                entry.get("project"),
                entry.get("kind"),
                int(time.time()),
                result["status_code"],
                result["ok"],
                result["latency_ms"],
                result["error"],
                result["final_url"],
            ),
        )
        # And the history: one row per check. The table above keeps the
        # current state and forgets the previous check; this one keeps it, so
        # "it just went down" can be answered at all.
        conn.execute(
            """
            INSERT INTO live_url_checks
                (name, url, project, kind, checked_at, status_code, ok, latency_ms, error, final_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["name"],
                entry["url"],
                entry.get("project"),
                entry.get("kind"),
                int(time.time()),
                result["status_code"],
                result["ok"],
                result["latency_ms"],
                result["error"],
                result["final_url"],
            ),
        )


def run(urls: Iterable[dict] | None = None) -> dict:
    ensure_schema()
    urls = list(urls) if urls is not None else _configured_urls()
    counters = {"checked": 0, "ok": 0, "errors": 0}
    with httpx.Client(http2=False) as client:
        for entry in urls:
            result = _check(client, entry)
            # Detect state change vs previous check
            prev_ok = None
            with db.db() as conn:
                row = conn.execute(
                    "SELECT ok FROM live_urls WHERE name = ? AND checked_at < ? ORDER BY checked_at DESC LIMIT 1",
                    (entry["name"], int(time.time())),
                ).fetchone()
                if row:
                    prev_ok = bool(row["ok"])
            _record(entry, result)
            counters["checked"] += 1
            counters["ok" if result["ok"] else "errors"] += 1
            icon = "🟢" if result["ok"] else "🔴"
            sc = result["status_code"] or "—"
            ms = result["latency_ms"]
            print(f"  {icon} {entry['name']:<32}  {str(sc):<5}  {ms}ms  {entry['url']}")
            if result["error"]:
                print(f"     error: {result['error']}")
            # Fire webhook on state change
            if prev_ok is not None and prev_ok != result["ok"]:
                # Record the change in url_changes table
                try:
                    from_status = "ok" if prev_ok else "fail"
                    to_status = "ok" if result["ok"] else "fail"
                    change_id = db.record_url_change(
                        url_name=entry["name"],
                        from_status=from_status,
                        to_status=to_status,
                        status_code=result["status_code"],
                        latency_ms=result["latency_ms"],
                    )
                except Exception:
                    change_id = None
                try:
                    from . import webhooks
                    if result["ok"]:
                        webhooks.url_recovered(entry["name"])
                    else:
                        webhooks.url_failed(entry["name"], result["status_code"])
                    if change_id is not None:
                        db.mark_url_change_notified(change_id)
                except Exception:
                    pass
    print(f"live_urls: checked={counters['checked']} ok={counters['ok']} errors={counters['errors']}")
    return counters


def latest() -> list[dict]:
    """Return latest snapshot per URL (most recent check)."""
    ensure_schema()
    with db.db() as conn:
        rows = conn.execute(
            """
            SELECT l.*
            FROM live_urls l
            JOIN (
                SELECT name, MAX(checked_at) AS m FROM live_urls GROUP BY name
            ) m ON m.name = l.name AND m.m = l.checked_at
            ORDER BY l.ok ASC, l.name ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def recent_unhealthy(hours: int = 24) -> list[dict]:
    """URLs that have had at least one failed check in the last N hours."""
    cutoff = int(time.time()) - hours * 3600
    with db.db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT name, project, url, status_code
               FROM live_urls
               WHERE checked_at >= ? AND (ok = 0 OR status_code IS NULL OR status_code = 0)
               ORDER BY name""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_url(name: str, url: str, *, project: str | None = None, kind: str | None = None) -> None:
    """Add or update a URL entry."""
    ensure_schema()
    with db.db() as conn:
        conn.execute(
            """
            INSERT INTO live_urls (name, url, project, kind, checked_at, ok, final_url)
            VALUES (?, ?, ?, ?, 0, 0, ?)
            ON CONFLICT(name) DO UPDATE SET url = excluded.url, project = excluded.project, kind = excluded.kind, final_url = excluded.final_url
            """,
            (name, url, project, kind, url),
        )


def changes_since(since_ts: int) -> list[dict]:
    """The checks made since `since_ts` whose result differs from the check
    before them.

    Useful for: 'what changed since last time I looked?'.
    """
    ensure_schema()
    with db.db() as conn:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY name ORDER BY checked_at DESC, id DESC) AS rn
                FROM live_url_checks
            )
            SELECT c.* FROM live_url_checks c
            JOIN ranked r ON r.id = c.id
            LEFT JOIN ranked p ON p.name = c.name AND p.rn = r.rn + 1
            WHERE c.checked_at >= ? AND p.id IS NOT NULL AND p.ok <> c.ok
            ORDER BY c.checked_at DESC, c.id DESC
            """,
            (since_ts,),
        ).fetchall()
    out = []
    for r in rows:
        out.append(dict(r))
    return out


def status_changes() -> list[dict]:
    """Latest check per URL vs the previous check of the same URL.

    Returns rows with a 'delta' field: 'recovered', 'failed', 'new', 'unchanged'.

    The comparison is made over the history of checks. The current-state table
    keeps one row per URL, so it cannot say whether a URL just went down: the
    check before the current one is not there any more.
    """
    ensure_schema()
    with db.db() as conn:
        # The last two checks of every URL that has a history.
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY name ORDER BY checked_at DESC, id DESC) AS rn
                FROM live_url_checks
            )
            SELECT a.name, a.url, a.project, a.status_code, a.ok, a.checked_at,
                   b.status_code AS prev_status, b.ok AS prev_ok, b.checked_at AS prev_checked
            FROM ranked a
            LEFT JOIN ranked b ON b.name = a.name AND b.rn = 2
            WHERE a.rn = 1
            ORDER BY a.name
            """
        ).fetchall()
        # A URL that has never been checked has no history: it is new.
        fresh = conn.execute(
            """
            SELECT c.name, c.url, c.project, c.status_code, c.ok, c.checked_at
            FROM live_urls c
            WHERE c.checked_at > 0
              AND NOT EXISTS (SELECT 1 FROM live_url_checks h WHERE h.name = c.name)
            ORDER BY c.name
            """
        ).fetchall()
    out = []
    for r in rows:
        prev_ok = r["prev_ok"]
        cur_ok = r["ok"]
        if prev_ok is None:
            delta = "new"
        elif cur_ok and not prev_ok:
            delta = "recovered"
        elif not cur_ok and prev_ok:
            delta = "failed"
        else:
            delta = "unchanged"
        out.append({
            "name": r["name"], "project": r["project"], "url": r["url"],
            "status_code": r["status_code"], "ok": bool(cur_ok), "checked_at": r["checked_at"],
            "prev_status": r["prev_status"], "prev_ok": bool(prev_ok) if prev_ok is not None else None,
            "prev_checked_at": r["prev_checked"], "delta": delta,
        })
    for r in fresh:
        out.append({
            "name": r["name"], "project": r["project"], "url": r["url"],
            "status_code": r["status_code"], "ok": bool(r["ok"]), "checked_at": r["checked_at"],
            "prev_status": None, "prev_ok": None, "prev_checked_at": None, "delta": "new",
        })
    out.sort(key=lambda d: d["name"])
    return out
