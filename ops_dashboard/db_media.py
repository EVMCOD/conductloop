"""Media assets and media jobs.

Split out of db.py (2026-10-23) alongside the media engine. Re-exported from
``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod().db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

import json
import time

def _dbmod():
    """The db module, imported lazily.

    A module-level ``from ops_dashboard import db`` makes this module depend on
    the import order: imported first, it breaks with a circular import
    (db.py re-exports from here at its end). 2026-10-23.
    """
    from ops_dashboard import db
    return db

# ============================================================
# Media assets — one row per generated file
# ============================================================


def add_asset(*, kind: str, prompt: str | None = None, project_key: str | None = None,
              engine: str | None = None, model: str | None = None,
              path: str | None = None, source_url: str | None = None,
              node_id: str | None = None, status: str = "ready",
              width: int | None = None, height: int | None = None,
              duration_sec: float | None = None, bytes_: int | None = None,
              meta: dict | None = None) -> int:
    now = int(time.time())
    with _dbmod().db() as conn:
        cur = conn.execute(
            "INSERT INTO media_assets (kind, project_key, prompt, engine, model, path, "
            "source_url, node_id, status, width, height, duration_sec, bytes, meta, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, project_key, prompt, engine, model, path, source_url, node_id, status,
             width, height, duration_sec, bytes_, json.dumps(meta or {}), now, now),
        )
        return int(cur.lastrowid or 0)


def get_asset(asset_id: int) -> dict | None:
    with _dbmod().db() as conn:
        row = conn.execute("SELECT * FROM media_assets WHERE id = ?", (int(asset_id),)).fetchone()
    if not row:
        return None
    return _row_to_asset(dict(row))


def list_assets(*, project_key: str | None = None, kind: str | None = None,
                limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM media_assets WHERE 1=1"
    params: list = []
    if project_key:
        sql += " AND project_key = ?"; params.append(project_key)
    if kind:
        sql += " AND kind = ?"; params.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with _dbmod().db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_asset(dict(r)) for r in rows]


def delete_asset(asset_id: int) -> bool:
    with _dbmod().db() as conn:
        cur = conn.execute("DELETE FROM media_assets WHERE id = ?", (int(asset_id),))
        return cur.rowcount > 0


def _row_to_asset(d: dict) -> dict:
    try: d["meta"] = json.loads(d.get("meta") or "{}")
    except Exception: d["meta"] = {}
    # The gallery renders /api/media/file/<id>, never the path on disk: the
    # client has no business knowing where anything lives, and a missing file
    # should show as a broken image rather than a 404 path.
    d["url"] = f"/api/media/file/{d.get('id')}"
    d["kind_label"] = {"image": "image", "video": "video",
                       "audio": "audio"}.get(d.get("kind"), d.get("kind"))
    return d


# ============================================================
# Media jobs — the async side of video generation
# ============================================================


def add_job(*, kind: str, prompt: str | None = None, project_key: str | None = None,
            engine: str | None = None, model: str | None = None,
            external_task_id: str | None = None, status: str = "queued",
            progress: int = 0, asset_id: int | None = None, error: str | None = None,
            meta: dict | None = None) -> int:
    now = int(time.time())
    with _dbmod().db() as conn:
        cur = conn.execute(
            "INSERT INTO media_jobs (kind, project_key, prompt, engine, model, "
            "external_task_id, status, progress, asset_id, error, meta, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, project_key, prompt, engine, model, external_task_id, status,
             int(progress), asset_id, error, json.dumps(meta or {}), now, now),
        )
        return int(cur.lastrowid or 0)


def get_job(job_id: int) -> dict | None:
    with _dbmod().db() as conn:
        row = conn.execute("SELECT * FROM media_jobs WHERE id = ?", (int(job_id),)).fetchone()
    if not row:
        return None
    return _row_to_job(dict(row))


def list_jobs(*, project_key: str | None = None, status: str | None = None,
              limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM media_jobs WHERE 1=1"
    params: list = []
    if project_key:
        sql += " AND project_key = ?"; params.append(project_key)
    if status:
        sql += " AND status = ?"; params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with _dbmod().db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_job(dict(r)) for r in rows]


def get_job_by_external_task(external_task_id: str) -> dict | None:
    with _dbmod().db() as conn:
        row = conn.execute(
            "SELECT * FROM media_jobs WHERE external_task_id = ? ORDER BY id DESC LIMIT 1",
            (str(external_task_id),),
        ).fetchone()
    if not row:
        return None
    return _row_to_job(dict(row))


def update_job(job_id: int, **fields) -> bool:
    allowed = {"status", "progress", "asset_id", "error", "external_task_id"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?"); params.append(v)
    if "meta" in fields and fields["meta"] is not None:
        sets.append("meta = ?"); params.append(json.dumps(fields["meta"]))
    if not sets:
        return False
    sets.append("updated_at = ?"); params.append(int(time.time())); params.append(int(job_id))
    with _dbmod().db() as conn:
        cur = conn.execute(f"UPDATE media_jobs SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def _row_to_job(d: dict) -> dict:
    try: d["meta"] = json.loads(d.get("meta") or "{}")
    except Exception: d["meta"] = {}
    return d


# ============================================================
# Provider configuration — who does the work, and with what
# ============================================================

def list_provider_configs() -> list[dict]:
    with _dbmod().db() as conn:
        rows = conn.execute(
            "SELECT * FROM media_providers ORDER BY priority ASC, key ASC"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["config"] = json.loads(d.get("config_json") or "{}")
        except Exception:
            d["config"] = {}
        d.pop("config_json", None)
        out.append(d)
    return out


def get_provider_config(key: str) -> dict | None:
    with _dbmod().db() as conn:
        row = conn.execute("SELECT * FROM media_providers WHERE key = ?",
                           (str(key),)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["config"] = json.loads(d.get("config_json") or "{}")
    except Exception:
        d["config"] = {}
    d.pop("config_json", None)
    return d


def set_provider_config(key: str, config: dict, *, enabled: bool | None = None,
                        priority: int | None = None) -> bool:
    """Store a provider's configuration, keeping the fields it did not change.

    The api_key is write-only from the API's point of view: it is never sent
    back to the client, and a request that omits it leaves the stored one
    alone.
    """
    now = int(time.time())
    current = get_provider_config(key) or {}
    merged = dict(current.get("config") or {})
    for k, v in (config or {}).items():
        if k == "api_key" and not str(v or "").strip():
            continue          # an empty key means "keep the one I have"
        merged[k] = v
    with _dbmod().db() as conn:
        conn.execute(
            "INSERT INTO media_providers (key, enabled, priority, config_json, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled, "
            "priority=excluded.priority, config_json=excluded.config_json, "
            "updated_at=excluded.updated_at",
            (str(key),
             1 if (enabled if enabled is not None else current.get("enabled", 0)) else 0,
             int(priority if priority is not None else current.get("priority", 100)),
             json.dumps(merged), now),
        )
    return True


def set_provider_state(key: str, *, enabled: bool | None = None,
                       last_error: str | None = None) -> bool:
    """Turn a provider on or off.

    It has to work for a provider that was never configured: "switch the
    local one on" is the first thing a user does, and that is exactly the
    case where there is no row yet.
    """
    now = int(time.time())
    row = get_provider_config(key)
    if row is None:
        # A brand new row: the provider's own default decides, so a provider
        # that works out of the box is not switched off by being touched.
        if enabled is None:
            try:
                from ops_dashboard import media_providers as mp

                spec = mp.spec_for(key)
                enabled = bool(spec.default_enabled) if spec else False
            except Exception:
                enabled = False
        with _dbmod().db() as conn:
            conn.execute(
                "INSERT INTO media_providers (key, enabled, last_error, checked_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                (str(key), 1 if enabled else 0, last_error, now, now),
            )
        return True
    with _dbmod().db() as conn:
        conn.execute(
            "UPDATE media_providers SET enabled=COALESCE(?, enabled), "
            "last_error=?, checked_at=?, updated_at=? WHERE key=?",
            (None if enabled is None else (1 if enabled else 0),
             last_error, now, now, str(key)),
        )
    return True


# ── provider usage: the counters and the brake ─────────────────────────────

def provider_usage_today() -> dict[str, dict[str, int]]:
    """What each provider did today.

    The counters are what make a cap useful: the user can see the brake
    coming instead of finding out when a render is refused.
    """
    today = time.strftime("%Y-%m-%d")
    with _dbmod().db() as conn:
        rows = conn.execute(
            "SELECT provider_key, calls, errors FROM media_provider_usage WHERE day=?",
            (today,),
        ).fetchall()
    return {r["provider_key"]: {"calls": int(r["calls"]), "errors": int(r["errors"])}
            for r in rows}


def record_provider_usage(key: str, *, ok: bool = True) -> None:
    """Count one call against a provider."""
    today = time.strftime("%Y-%m-%d")
    now = int(time.time())
    with _dbmod().db() as conn:
        conn.execute(
            "INSERT INTO media_provider_usage (provider_key, day, calls, errors, updated_at) "
            "VALUES (?,?,1,?,?) ON CONFLICT(provider_key, day) DO UPDATE SET "
            "calls = calls + 1, errors = errors + ?, updated_at = ?",
            (str(key), today, 0 if ok else 1, now,
             0 if ok else 1, now),
        )


def provider_usage_since(key: str, day: str) -> int:
    """How many calls a provider took on a given day."""
    with _dbmod().db() as conn:
        row = conn.execute(
            "SELECT calls FROM media_provider_usage WHERE provider_key=? AND day=?",
            (str(key), day),
        ).fetchone()
    return int(row["calls"]) if row else 0
