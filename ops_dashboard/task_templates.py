"""Task templates: reusable checklists / workflows.

Each template has a `key`, a display `name`, an `icon`, and a list of items.
Templates are persisted in the `task_templates` table.

Default templates ship with the dashboard and can be instantiated via:
  ops templates                              # list all
  ops templates show launch_checklist        # show one
  ops templates run launch_checklist         # create user_todos from it

Or via the UI: Templates view → "Run" button per template.
"""
from __future__ import annotations

from . import db

DEFAULT_TEMPLATES: dict[str, dict] = {
    "launch_checklist": {
        "name": "Launch checklist",
        "icon": "🚀",
        "description": "End-to-end checklist for shipping a new product to production.",
        "items": [
            {"title": "Buy domain", "description": "Pick short, brandable, .com if possible"},
            {"title": "Set DNS", "description": "Point domain to deployment target (Cloudflare, Vercel, etc.)"},
            {"title": "Deploy to production", "description": "Run final deploy, verify with curl"},
            {"title": "SSL cert", "description": "Auto via Cloudflare; manual certs need renewal calendar entry"},
            {"title": "SEO basics", "description": "sitemap.xml, robots.txt, OG tags, JSON-LD, canonical, hreflang"},
            {"title": "Analytics", "description": "Plausible or GA4, conversion goal defined"},
            {"title": "Privacy policy + Terms", "description": "Generate or copy from template; link in footer"},
            {"title": "Monitoring", "description": "Uptime monitor (BetterStack, UptimeRobot, or cron + webhook)"},
            {"title": "Backup plan", "description": "DB dump cron, R2/S3 backup, restore tested"},
            {"title": "Social announcement", "description": "X/LinkedIn post, ProductHunt optional, indiehackers"},
            {"title": "Post-launch review", "description": "7 days after: check analytics, fix top bugs, ask for feedback"},
        ],
    },
    "weekly_review": {
        "name": "Weekly review",
        "icon": "📅",
        "description": "Friday end-of-week reflection on what shipped and what's next.",
        "items": [
            {"title": "List commits shipped this week", "description": "git log --since='last monday' --oneline per repo"},
            {"title": "Review open TODOs", "description": "Anything stalled >7 days? Reassign or drop."},
            {"title": "Check live URLs", "description": "All green? Any new latency regressions?"},
            {"title": "Review costs", "description": "Cloudflare / Vercel / AI / domains — within budget?"},
            {"title": "Plan next week (3 priorities)", "description": "Don't try to do everything. Pick 3 max."},
            {"title": "Update MEMORY.md", "description": "Decisions, lessons, project status changes"},
            {"title": "Take a snapshot", "description": "ops snapshot take (auto on index, but verify)"},
        ],
    },
    "incident_response": {
        "name": "Incident response",
        "icon": "🚨",
        "description": "When a URL goes down or a critical bug ships. Fill in as you go.",
        "items": [
            {"title": "Identify scope", "description": "One URL? All projects? Customer-facing?"},
            {"title": "Notify (if customer-facing)", "description": "Status page or quick post; better than silence"},
            {"title": "Reproduce", "description": "curl, browser incognito, or test env. Get exact failure."},
            {"title": "Roll back (if urgent)", "description": "git revert + redeploy. Don't debug in prod."},
            {"title": "Root cause", "description": "Logs, deploy diff, dependency change, config drift"},
            {"title": "Fix forward", "description": "Test on preview, then prod. Verify health endpoint."},
            {"title": "Postmortem (within 48h)", "description": "Timeline, root cause, prevention. Save in vault."},
            {"title": "Update monitoring", "description": "Could we have caught this earlier? Add the alert."},
        ],
    },
    "new_project_setup": {
        "name": "New project setup",
        "icon": "✨",
        "description": "Bootstrap a new personal project from scratch.",
        "items": [
            {"title": "Create repo + git init", "description": "On GitHub under EVMCOD org"},
            {"title": "Pick stack", "description": "Python/FastAPI or Next.js? Default to existing patterns."},
            {"title": "Add to projects registry", "description": "ops_dashboard/projects.py + playbook/"},
            {"title": "Set up auth", "description": "Better-Auth or NextAuth, email + Google OAuth"},
            {"title": "Set up DB", "description": "Neon (serverless) or local SQLite + Drizzle"},
            {"title": "Add first deploy", "description": "Cloudflare Pages (web) + Render/Railway (Python)"},
            {"title": "Add live URL to ops-dashboard", "description": "ops_dashboard/indexers/live_urls.py KNOWN_URLS"},
            {"title": "Write README + TODO", "description": "Why this exists, what's MVP, what's deferred"},
        ],
    },
    "seo_optimization": {
        "name": "SEO optimization",
        "icon": "🔍",
        "description": "Monthly SEO pass on a money page.",
        "items": [
            {"title": "Check Search Console", "description": "Impressions, clicks, average position, CTR"},
            {"title": "Top 10 keywords", "description": "Identify top queries, ensure each has a target page"},
            {"title": "Update title + meta description", "description": "Front-load the keyword, 150-160 chars"},
            {"title": "Add internal links", "description": "3-5 contextual links to related posts"},
            {"title": "Add FAQPage schema", "description": "3-5 Q&A in JSON-LD at the bottom"},
            {"title": "Check Core Web Vitals", "description": "Lighthouse: LCP <2.5s, CLS <0.1, INP <200ms"},
            {"title": "Refresh content", "description": "Update stats, add new sections, refresh publish date"},
        ],
    },
    "code_review": {
        "name": "Code review",
        "icon": "👀",
        "description": "When reviewing a PR or doing self-review before merge.",
        "items": [
            {"title": "Read the diff top-to-bottom", "description": "Don't skim. Understand intent before style."},
            {"title": "Check tests", "description": "New code = new tests. Coverage didn't drop?"},
            {"title": "Run linter + typecheck", "description": "No new warnings, no `any` slips"},
            {"title": "Check error handling", "description": "Edge cases, retries, graceful failure"},
            {"title": "Verify no secrets in code", "description": "API keys, tokens, .env files — none committed"},
            {"title": "Verify migrations are reversible", "description": "Down migration works on a copy of prod data"},
            {"title": "Check observability", "description": "Logs structured? Errors traceable? Metrics added?"},
            {"title": "Approve + leave 1 specific praise", "description": "Reviews shouldn't be all criticism."},
        ],
    },
}


def seed_defaults() -> int:
    """Insert/update default templates. Idempotent."""
    n = 0
    for key, t in DEFAULT_TEMPLATES.items():
        db.upsert_template(
            key=key,
            name=t["name"],
            items=t["items"],
            description=t.get("description"),
            icon=t.get("icon"),
        )
        n += 1
    return n


def ensure_seeded() -> None:
    """Seed if no templates exist yet."""
    if not db.list_templates():
        n = seed_defaults()
        print(f"task_templates: seeded {n} default templates")
    else:
        # Optionally update if any default was modified
        existing = {t["key"]: t for t in db.list_templates()}
        for key, t in DEFAULT_TEMPLATES.items():
            if key not in existing:
                db.upsert_template(key=key, name=t["name"], items=t["items"], description=t.get("description"), icon=t.get("icon"))
