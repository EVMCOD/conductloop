"""GitHub sync state.

Split out of db.py (2026-07-20). Re-exported from ``ops_dashboard.db`` so ``db.<name>`` callers keep working. Uses the shared
transaction context manager via ``_dbmod.db()`` (module-reference import,
safe mid-import).
"""
from __future__ import annotations

from ops_dashboard import db as _dbmod

# ============================================================
# GitHub sync
# ============================================================

def upsert_github_item(
    *,
    repo_full: str,
    item_type: str,
    url: str,
    item_number: int | None = None,
    title: str | None = None,
    state: str | None = None,
    author: str | None = None,
    created_at_remote: int | None = None,
    updated_at_remote: int | None = None,
) -> None:
    now = int(__import__("time").time())
    with _dbmod.db() as conn:
        conn.execute(
            """INSERT INTO github_items
               (repo_full, item_type, item_number, title, state, author, url,
                created_at_remote, updated_at_remote, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(repo_full, item_type, item_number, url) DO UPDATE SET
                 title = excluded.title,
                 state = excluded.state,
                 author = excluded.author,
                 updated_at_remote = excluded.updated_at_remote,
                 fetched_at = excluded.fetched_at
            """,
            (repo_full, item_type, item_number, title, state, author, url,
             created_at_remote, updated_at_remote, now),
        )


def list_github_items(
    *,
    repo: str | None = None,
    item_type: str | None = None,
    state: str | None = None,
    limit: int = 100,
) -> list[dict]:
    sql = "SELECT * FROM github_items WHERE 1=1"
    params: list = []
    if repo:
        sql += " AND repo_full = ?"
        params.append(repo)
    if item_type:
        sql += " AND item_type = ?"
        params.append(item_type)
    if state:
        sql += " AND state = ?"
        params.append(state)
    sql += " ORDER BY updated_at_remote DESC LIMIT ?"
    params.append(limit)
    with _dbmod.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def github_summary() -> dict:
    with _dbmod.db() as conn:
        by_repo = [dict(r) for r in conn.execute(
            """SELECT repo_full, item_type, state, COUNT(*) AS c
               FROM github_items GROUP BY repo_full, item_type, state
               ORDER BY repo_full"""
        ).fetchall()]
        open_prs = [dict(r) for r in conn.execute(
            """SELECT repo_full, item_number, title, url, updated_at_remote
               FROM github_items WHERE item_type='pr' AND state='open'
               ORDER BY updated_at_remote DESC LIMIT 20"""
        ).fetchall()]
        open_issues = [dict(r) for r in conn.execute(
            """SELECT repo_full, item_number, title, url, updated_at_remote
               FROM github_items WHERE item_type='issue' AND state='open'
               ORDER BY updated_at_remote DESC LIMIT 20"""
        ).fetchall()]
    return {"by_repo": by_repo, "open_prs": open_prs, "open_issues": open_issues}
