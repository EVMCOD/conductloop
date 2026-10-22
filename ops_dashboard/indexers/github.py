"""GitHub sync indexer.

Fetches open PRs, open issues, and recent commits for each known repo via
the public GitHub REST API. No auth needed for public repos (60 req/h).

For each repo in projects.PROJECTS that has a github_full attribute
(e.g. 'EVMCOD/traincurve'), we hit:
  - /repos/{owner}/{repo}/pulls?state=open
  - /repos/{owner}/{repo}/issues?state=open (filtered to actual issues, not PRs)
  - /repos/{owner}/{repo}/commits?per_page=20

Results are stored in the `github_items` table. Re-runs are idempotent.
"""
from __future__ import annotations

import os
import time
from datetime import datetime

import httpx

from .. import db
from .. import projects as projects_mod

API = "https://api.github.com"


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        from .. import app_paths
        cfg_path = str(app_paths.config_path())
        try:
            import json
            cfg = json.loads(open(cfg_path).read())
            token = cfg.get("GITHUB_TOKEN") or ""
        except Exception:
            pass
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get(path: str, params: dict | None = None, timeout: float = 10.0) -> list | dict | None:
    url = API + path
    try:
        r = httpx.get(url, headers=_headers(), params=params or {}, timeout=timeout, follow_redirects=True)
        if r.status_code == 403:
            # Rate-limited
            return None
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception:
        return None


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def sync_repo(full_name: str) -> dict:
    """Sync PRs, issues, and commits for a single repo."""
    counters = {"prs": 0, "issues": 0, "commits": 0}

    # Open PRs
    prs = _get(f"/repos/{full_name}/pulls", params={"state": "open", "per_page": 30})
    if isinstance(prs, list):
        for pr in prs:
            db.upsert_github_item(
                repo_full=full_name,
                item_type="pr",
                item_number=pr.get("number"),
                title=pr.get("title"),
                state="open",
                author=(pr.get("user") or {}).get("login"),
                url=pr.get("html_url"),
                created_at_remote=_ts(pr.get("created_at")),
                updated_at_remote=_ts(pr.get("updated_at")),
            )
            counters["prs"] += 1

    # Open issues (filter out PRs)
    issues = _get(f"/repos/{full_name}/issues", params={"state": "open", "per_page": 30})
    if isinstance(issues, list):
        for iss in issues:
            if "pull_request" in iss:
                continue  # PRs are returned by /issues too
            db.upsert_github_item(
                repo_full=full_name,
                item_type="issue",
                item_number=iss.get("number"),
                title=iss.get("title"),
                state="open",
                author=(iss.get("user") or {}).get("login"),
                url=iss.get("html_url"),
                created_at_remote=_ts(iss.get("created_at")),
                updated_at_remote=_ts(iss.get("updated_at")),
            )
            counters["issues"] += 1

    # Recent commits (last 20)
    commits = _get(f"/repos/{full_name}/commits", params={"per_page": 20})
    if isinstance(commits, list):
        for c in commits:
            sha = (c.get("sha") or "")[:7]
            url = (c.get("html_url") or "").split("#")[0]
            commit_data = (c.get("commit") or {})
            title = (commit_data.get("message") or "").split("\n", 1)[0][:200]
            db.upsert_github_item(
                repo_full=full_name,
                item_type="commit",
                item_number=None,
                title=f"{sha} {title}",
                state="committed",
                author=((c.get("author") or {}).get("login")) or (commit_data.get("author") or {}).get("name"),
                url=url,
                created_at_remote=_ts(c.get("commit", {}).get("author", {}).get("date")),
                updated_at_remote=_ts(c.get("commit", {}).get("committer", {}).get("date")),
            )
            counters["commits"] += 1

    return counters


def run(repos: list[str] | None = None) -> dict:
    """Sync GitHub data for all projects that have a github_repo attribute."""
    if not repos:
        repos = [p.github_repo for p in projects_mod.PROJECTS if getattr(p, "github_repo", None)]
    totals = {"repos": 0, "prs": 0, "issues": 0, "commits": 0, "skipped": []}
    for full in repos:
        if not full or "/" not in full:
            continue
        totals["repos"] += 1
        try:
            r = sync_repo(full)
            totals["prs"] += r["prs"]
            totals["issues"] += r["issues"]
            totals["commits"] += r["commits"]
            print(f"  ✓ {full}: {r['prs']} PRs, {r['issues']} issues, {r['commits']} commits")
        except Exception as e:
            totals["skipped"].append({"repo": full, "error": str(e)})
            print(f"  ! {full}: {e}")
        time.sleep(0.3)  # rate-limit politeness
    print(f"github: synced {totals['repos']} repos, {totals['prs']} PRs, {totals['issues']} issues, {totals['commits']} commits")
    return totals
