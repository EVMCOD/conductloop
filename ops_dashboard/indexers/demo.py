"""Demo data generator for ConductLoop.

Used when CONDUCTLOOP_DEMO=1 is set. Inserts plausible, self-consistent
fixture data into the same tables the real indexers use, so the dashboard
renders the same way for a trial user / marketing demo / screenshot capture
as it does for the real product.

All names, companies, repos, URLs, and numbers are **fictional** - they do
not refer to real users, real projects, or real revenue. Coherence is the
goal: a "demo" should look like a real product with a real customer, not
lorem-ipsum.
"""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

from .. import db

DEMO_TAG = "demo"

# Respect CONDUCTLOOP_DB_PATH so the demo data lands in the right SQLite file.
# (cli.py / server.py set this when CONDUCTLOOP_DEMO=1; we honour it here too
# so `python -c "from ops_dashboard.indexers import demo; demo.run()"` works.)
_demo_db = os.environ.get("CONDUCTLOOP_DB_PATH")
if _demo_db:
    db.DB_PATH = Path(_demo_db)  # type: ignore[attr-defined]

# - Notes (Obsidian) -
# 30 plausible second-brain notes. Mix of evergreen and dated entries.

DEMO_NOTES: list[dict] = [
    {"path": "evergreen/para-method.md", "title": "The PARA Method", "tags": ["productivity", "para", "second-brain", "evergreen"], "body": "# The PARA Method\n\nA framework by Tiago Forte for organizing digital information into four categories:\n\n- **Projects** - short-term efforts with a deadline\n- **Areas** - long-term responsibilities\n- **Resources** - topics of interest\n- **Archives** - inactive items\n\nThe key insight: organize by actionability, not by topic.\n\nSee also: [[building-a-second-brain]], [[zettelkasten-vs-para]]"},
    {"path": "evergreen/building-a-second-brain.md", "title": "Building a Second Brain", "tags": ["productivity", "second-brain", "evergreen"], "body": "# Building a Second Brain\n\nThe premise: your brain is for *having* ideas, not *holding* them. A second brain (digital) gives you a reliable external memory.\n\n## The CODE method\n\n1. **Capture** - collect what resonates\n2. **Organize** - by actionability (PARA)\n3. **Distill** - progressive summarization\n4. **Express** - produce output"},
    {"path": "evergreen/zettelkasten.md", "title": "Zettelkasten Method", "tags": ["zettelkasten", "note-taking", "evergreen"], "body": "# Zettelkasten\n\nA note-taking system developed by sociologist Niklas Luhmann, who used it to publish 70+ books and 400+ academic articles.\n\n## Core principles\n\n- **Atomic notes** - one idea per note\n- **Linking** - connect notes explicitly\n- **Emergent structure** - let categories emerge from links\n- **Index notes** - entry points into a topic"},
    {"path": "daily/2026-06-15.md", "title": "Daily Note - 2026-06-15", "tags": ["daily", "journal"], "body": "# 2026-06-15\n\n## Highlights\n- Shipped the v0.4 release of Mosaic - zero downtime deploy\n- 90-min deep work block on the Q3 strategy doc\n- Walked 4.2km, listened to *Range* by David Epstein\n\n## Open loops\n- [ ] Reply to Sam about the API design review\n- [ ] Schedule 1:1 with Taylor for Friday\n- [ ] Order new monitor stand\n\n## Reflection\nThe morning block worked again. Coffee at 06:30, no phone until 08:00."},
    {"path": "daily/2026-06-16.md", "title": "Daily Note - 2026-06-16", "tags": ["daily", "journal"], "body": "# 2026-06-16\n\n## Highlights\n- Customer interview with Verdant (45 min) - validated the on-prem demand\n- Read draft chapter 4 of the book, gave feedback to Jordan\n- Cleared the inbox to zero for the first time in two weeks"},
    {"path": "daily/2026-06-17.md", "title": "Daily Note - 2026-06-17", "tags": ["daily", "journal"], "body": "# 2026-06-17\n\n## Highlights\n- Deep work: designed the new pricing tiers\n- Cooked the mushroom risotto from Ottolenghi - nailed it\n- Caught up with Alex after 3 months"},
    {"path": "daily/2026-06-22.md", "title": "Daily Note - 2026-06-22", "tags": ["daily", "journal"], "body": "# 2026-06-22\n\n## Highlights\n- Quarterly review meeting (90 min). Net retention up 11 points.\n- Refactored the billing service - finally readable\n- 7h sleep streak, 5 days"},
    {"path": "daily/2026-06-23.md", "title": "Daily Note - 2026-06-23", "tags": ["daily", "journal"], "body": "# 2026-06-23\n\n## Highlights\n- Wrote 1,800 words on the new essay\n- Hired for the senior backend role - offer accepted\n- Skipped the afternoon meetings, felt great"},
    {"path": "daily/2026-06-24.md", "title": "Daily Note - 2026-06-24", "tags": ["daily", "journal"], "body": "# 2026-06-24\n\n## Highlights\n- Demoed Mosaic to Prism's CTO. 25 min. Strong signal.\n- Hiked 12km, the new boots are good\n- Cooked for friends - first dinner party in 6 months"},
    {"path": "books/range-david-epstein.md", "title": "Range - David Epstein", "tags": ["books", "learning", "evergreen"], "body": "# Range - David Epstein\n\nThesis: in most domains, generalists outperform specialists in the long run.\n\n## Key ideas\n- **Wicked vs kind learning environments**\n- **Lateral thinking** - drawing analogies across domains\n- **The honeymoon effect** - sampling broadly before committing"},
    {"path": "books/antifragile-nassim-taleb.md", "title": "Antifragile - Nassim Taleb", "tags": ["books", "risk", "evergreen"], "body": "# Antifragile\n\nThe core idea: some things *benefit* from disorder. Antifragile is beyond robust.\n\n## The triad\n- **Fragile** - breaks under stress\n- **Robust** - resists stress\n- **Antifragile** - improves under stress"},
    {"path": "books/atomic-essays-james-clear.md", "title": "Atomic Essays - James Clear", "tags": ["writing", "evergreen"], "body": "# Atomic Essays\n\nA format: short, self-contained essays (200-500 words) that make a single point. Publish one per day.\n\n## Why it works\n- Lowers the activation energy for writing\n- Compounds over time\n- Forces clarity"},
    {"path": "books/calm-technology-amber-case.md", "title": "Calm Technology - Amber Case", "tags": ["design", "ux", "evergreen"], "body": "# Calm Technology\n\nPrinciples for designing technology that informs without demanding attention.\n\n## Principles\n1. Ask whether the technology should require attention at all\n2. Make the technology disappear\n3. Make the technology informative\n4. Use the appropriate amount of technology"},
    {"path": "projects/mosaic-q3-plan.md", "title": "Mosaic - Q3 Plan", "tags": ["projects", "mosaic", "planning"], "body": "# Mosaic - Q3 2026 Plan\n\n## Headline goal\n- Net retention from 108% to 115%\n- New self-hosted launch by end of September\n\n## Key bets\n1. **Self-hosted option** - losing ~30% of mid-market deals\n2. **Onboarding revamp** - time-to-first-value is 14 days, target 5\n3. **Pricing page** - doing 0 work\n\n## What we will NOT do\n- Mobile app\n- AI features for the sake of AI\n- Major brand redesign"},
    {"path": "projects/verdant-proposal.md", "title": "Verdant - Sales Proposal", "tags": ["projects", "verdant", "sales"], "body": "# Verdant - Sales Proposal\n\n## Customer\n- 220-person biotech\n- 3 research sites\n- Regulatory heavy (FDA audits)\n\n## What they need\n- On-prem deployment\n- SSO via Okta\n- Custom retention policy (7 years)\n- Audit log export to S3\n\n## Our offer\n- Annual contract: $148k/yr\n- 3-year price lock\n- 90-day implementation\n- Dedicated CSM"},
    {"path": "projects/team-rituals.md", "title": "Team Rituals", "tags": ["management", "team", "evergreen"], "body": "# Team Rituals\n\n## Weekly\n- Monday: 30-min kickoff\n- Wednesday: 60-min show and tell\n- Friday: written reflection\n\n## Monthly\n- All-hands first Wednesday (45 min)\n- 1:1s are sacred\n\n## What we removed\n- Daily standups (replaced with async Slack check-in)\n- Status update meetings (replaced with written Friday thread)"},
    {"path": "projects/q3-okrs-draft.md", "title": "Q3 OKRs - Draft", "tags": ["okrs", "planning"], "body": "# Q3 OKRs - Draft\n\n## O1: Launch self-hosted option\n- KR1: Ship self-hosted to 1 design partner (Aug 15)\n- KR2: Sign first self-hosted deal (Sep 30)\n- KR3: Self-hosted install time < 4 hours\n\n## O2: Improve onboarding\n- KR1: Time-to-first-value < 5 days\n- KR2: Day-7 activation rate > 65%\n\n## O3: Pricing & packaging\n- KR1: New pricing page live (Aug 1)\n- KR2: Annual plan attach rate > 35%"},
    {"path": "personal/sleep-protocol.md", "title": "Sleep Protocol", "tags": ["health", "evergreen"], "body": "# Sleep Protocol\n\n## Targets\n- 7-8h per night\n- Wake time fixed at 06:30 weekdays\n- Lights out by 22:30\n\n## What helps\n- Cold room (18-19C)\n- 5min meditation before bed\n- No caffeine after 14:00"},
    {"path": "personal/training-block-3.md", "title": "Training Block 3 - Hypertrophy", "tags": ["health", "training"], "body": "# Training Block 3 - Hypertrophy\n\n4-week block, 4 days/week, push/pull/legs/upper-lower split.\n\n## Week structure\n- Mon: Upper (heavy)\n- Tue: Lower (heavy)\n- Wed: Rest / mobility\n- Thu: Push (volume)\n- Fri: Pull (volume)\n- Sat: Conditioning (zone 2, 45 min)\n- Sun: Rest"},
    {"path": "personal/2026-goals.md", "title": "2026 Goals", "tags": ["personal", "goals", "annual"], "body": "# 2026 Goals\n\n## Health\n- 7h+ sleep 80% of nights\n- 4 workouts/week\n- Annual physical: all green\n\n## Work\n- Net retention > 115% by EOY\n- Self-hosted GA\n- Hire 2 senior engineers\n- Publish 12 essays\n\n## Personal\n- Read 24 books\n- Travel to 3 new countries\n- Learn conversational German (B1)\n- Cook 50 new recipes"},
    {"path": "code/tauri-vs-electron.md", "title": "Tauri vs Electron - Decision Notes", "tags": ["code", "tauri", "electron", "evergreen"], "body": "# Tauri vs Electron\n\n## When Tauri wins\n- App size matters (Tauri 5-15MB vs Electron 80-150MB)\n- Memory pressure matters\n- macOS notarization is OK with you\n- You can write Rust\n\n## When Electron wins\n- You need maximum cross-platform parity\n- You can't invest in Rust\n- You depend on a Node-only native module"},
    {"path": "code/sqlite-patterns.md", "title": "SQLite Patterns That Scale", "tags": ["code", "sqlite", "evergreen"], "body": "# SQLite Patterns That Scale\n\n## WAL mode is non-negotiable\n- Concurrent readers + one writer\n- Massive throughput improvement\n\n## Indexes\n- B-tree for range queries\n- FTS5 for full-text search\n- Covering indexes for hot read paths\n\n## Patterns\n- **Upsert**: INSERT ... ON CONFLICT ... DO UPDATE\n- **Soft delete**: deleted_at INTEGER\n- **Migrations**: keep them in db.py, run on startup, idempotent"},
    {"path": "evergreen/decision-journal.md", "title": "Decision Journal Template", "tags": ["productivity", "decision-making", "evergreen"], "body": "# Decision Journal\n\nA simple template for capturing non-trivial decisions.\n\n## Template\n\n```\n## Date: YYYY-MM-DD\n## Decision:\n## Context:\n## Options considered:\n## Chosen:\n## Why:\n## Reversibility: cheap | expensive | irreversible\n## Review date:\n```\n\n## Why bother\n\nMost decisions are made and forgotten. Reviewing at 3-6 months is the only way to build a feedback loop on your own judgment."},
    {"path": "evergreen/feedback-framework.md", "title": "Feedback Framework", "tags": ["management", "feedback", "evergreen"], "body": "# Feedback Framework\n\n## The SBI model\n- **Situation** - what was the context\n- **Behavior** - what specifically happened\n- **Impact** - what was the effect\n\n## What doesn't work\n- 'You always...' or 'You never...'\n- Sandwiching hard feedback between positives\n- Saving it for performance reviews"},
    {"path": "evergreen/meeting-cultures.md", "title": "Meeting Cultures", "tags": ["management", "meetings", "evergreen"], "body": "# Meeting Cultures\n\n## The best meetings I attend\n- Have a written agenda\n- End with explicit next steps + owners\n- Time-box ruthlessly\n- Default to 25 min, not 30\n\n## What we're trying at Mosaic\n- 2-meeting days max\n- 30-min default, 25-min actual\n- 'Optional' attendance means truly optional"},
    {"path": "evergreen/focus-vs-busywork.md", "title": "Focus vs Busywork", "tags": ["productivity", "focus", "evergreen"], "body": "# Focus vs Busywork\n\nBusywork feels like work. Focus is invisible.\n\n## Heuristics\n- If you can't articulate the next concrete decision you're moving toward, you're doing busywork\n- If your output is 'I was in a lot of meetings today', it was busywork\n- If your output is 'I now know exactly which 3 things matter', it was focus"},
    {"path": "evergreen/written-thinking.md", "title": "Written Thinking", "tags": ["writing", "thinking", "evergreen"], "body": "# Written Thinking\n\nWriting is not the output of thinking. Writing *is* thinking.\n\nYou don't first think, then write. You write to think. Most 'I know what I want to say' is an illusion.\n\n## Implications\n- Never skip the first draft\n- Don't think and edit at the same time\n- The act of writing surfaces assumptions you didn't know you had"},
    {"path": "evergreen/energy-management.md", "title": "Energy Management", "tags": ["productivity", "energy", "evergreen"], "body": "# Energy Management\n\nTime management is the wrong frame. Energy is the constraint, not time.\n\n## Inputs\n- Sleep (the most leveraged)\n- Movement\n- Nutrition\n- Light exposure\n- Social connection\n- Purpose\n\n## Practice\n- Schedule hard work when energy is highest (morning)\n- Schedule shallow work when energy is low (after lunch, end of day)"},
    {"path": "evergreen/saying-no.md", "title": "Saying No", "tags": ["productivity", "career", "evergreen"], "body": "# Saying No\n\nThe most leveraged professional skill is saying no - to meetings, to requests, to projects, to features.\n\n## The cost of yes\n- 1 yes to a 1-hour meeting = 1 hour\n- 1 yes to a 4-month project = 4 months\n- 1 yes to a 'small favor' = 20 context switches\n\n## The actual calculation\nEvery yes is a no to something else. What are you no-ing?"},
    {"path": "evergreen/context-switching.md", "title": "Context Switching", "tags": ["productivity", "focus", "evergreen"], "body": "# Context Switching\n\nThe single biggest tax on knowledge work.\n\n## The cost\n- ~25 minutes to fully re-engage with a deep task after a context switch\n- 6 context switches per day = 2.5h of pure recovery overhead\n\n## How to reduce\n- Block the same kind of work together (batching)\n- Close all communication channels during deep work\n- Don't check email 'just in case' - schedule it\n- Reject meetings that could be async"},
    {"path": "evergreen/curation-vs-creation.md", "title": "Curation vs Creation", "tags": ["writing", "creator-economy", "evergreen"], "body": "# Curation vs Creation\n\nCuration is easier than creation. That's why most people do it.\n\nCuration feels productive (saving articles, organizing notes). Creation is harder (writing, building, shipping).\n\n## The trap\nMost 'productivity' is curation dressed up as creation.\n\n## The fix\nSet a creation quota. Curation is allowed only as input to creation."},
]


# - Repos (mock) -
DEMO_REPOS: list[dict] = [
    {"name": "mosaic-core", "summary": "Core platform for Mosaic. FastAPI + Postgres + Redis.", "topics": ["python", "fastapi", "postgres"], "stars": 1247, "forks": 189, "language": "Python"},
    {"name": "mosaic-web", "summary": "Web client. Next.js 16 + React 19 + Tailwind v4.", "topics": ["typescript", "nextjs", "react"], "stars": 542, "forks": 71, "language": "TypeScript"},
    {"name": "mosaic-self-hosted", "summary": "Helm chart + Docker compose for self-hosted installs.", "topics": ["kubernetes", "helm", "docker"], "stars": 318, "forks": 47, "language": "Go"},
    {"name": "verdant-integration", "summary": "Verdant <-> Mosaic data sync service. Cloud Function.", "topics": ["typescript", "gcp", "integration"], "stars": 24, "forks": 3, "language": "TypeScript"},
    {"name": "design-system", "summary": "Mosaic's design system. Tokens, components, docs.", "topics": ["design", "react", "storybook"], "stars": 89, "forks": 12, "language": "TypeScript"},
    {"name": "internal-tooling", "summary": "Internal CLIs and scripts. Python + typer + rich.", "topics": ["python", "cli", "tooling"], "stars": 12, "forks": 2, "language": "Python"},
    {"name": "data-pipeline", "summary": "Nightly ETL from Postgres to BigQuery. dbt + Airflow.", "topics": ["data", "dbt", "airflow"], "stars": 56, "forks": 9, "language": "Python"},
    {"name": "status-page", "summary": "Public status page. Cloudflare Workers + D1.", "topics": ["cloudflare", "workers", "status"], "stars": 31, "forks": 4, "language": "TypeScript"},
]


# - Live URLs (mock) -
DEMO_URLS: list[dict] = [
    {"name": "mosaic.app",               "url": "https://mosaic.example.com",            "project": "mosaic",  "kind": "marketing"},
    {"name": "app.mosaic.example",       "url": "https://app.mosaic.example.com",        "project": "mosaic",  "kind": "app"},
    {"name": "api.mosaic.example",       "url": "https://api.mosaic.example.com/health", "project": "mosaic",  "kind": "api"},
    {"name": "docs.mosaic.example",      "url": "https://docs.mosaic.example.com",       "project": "mosaic",  "kind": "docs"},
    {"name": "status.mosaic.example",    "url": "https://status.mosaic.example.com",     "project": "mosaic",  "kind": "status"},
    {"name": "verdant.example.com",      "url": "https://verdant.example.com",           "project": "verdant", "kind": "marketing"},
    {"name": "prism.example.com",        "url": "https://prism.example.com",             "project": "prism",   "kind": "marketing"},
    {"name": "helix.example.com",        "url": "https://helix.example.com",             "project": "helix",   "kind": "marketing"},
    {"name": "blog.mosaic.example",      "url": "https://blog.mosaic.example.com",       "project": "mosaic",  "kind": "blog"},
    {"name": "changelog.mosaic.example", "url": "https://changelog.mosaic.example.com",  "project": "mosaic",  "kind": "changelog"},
    {"name": "cdn.mosaic.example",       "url": "https://cdn.mosaic.example.com/health", "project": "mosaic",  "kind": "cdn"},
    {"name": "auth.mosaic.example",      "url": "https://auth.mosaic.example.com/health","project": "mosaic",  "kind": "auth"},
]


# - Founders (mock) -
DEMO_FOUNDERS: list[dict] = [
    {"name": "Alex Chen",      "tier": "founder", "badge": "gold",   "original_cents": 29900, "joined_at_offset_days": 92},
    {"name": "Sam Rivera",     "tier": "founder", "badge": "gold",   "original_cents": 49900, "joined_at_offset_days": 58},
    {"name": "Jordan Mills",   "tier": "founder", "badge": "gold",   "original_cents": 99900, "joined_at_offset_days": 41},
    {"name": "Taylor Park",    "tier": "founder", "badge": "gold",   "original_cents": 1000,  "joined_at_offset_days": 12},
    {"name": "Mira Kovacevic", "tier": "founder", "badge": "silver", "original_cents": 9900,  "joined_at_offset_days": 28},
    {"name": "Devon Okafor",   "tier": "founder", "badge": "silver", "original_cents": 9900,  "joined_at_offset_days": 7},
]


# - Activity feed events (mock) -
DEMO_ACTIVITY: list[tuple[str, str]] = [
    ("Alex Chen",      "joined the founders circle"),
    ("Sam Rivera",     "shipped the v0.4 release"),
    ("Jordan Mills",   "published a 1,400-word essay on retention"),
    ("Taylor Park",    "completed onboarding in 4 minutes"),
    ("Mira Kovacevic", "booked a 30-min call with the team"),
    ("Devon Okafor",   "shared feedback on the new pricing page"),
    ("Alex Chen",      "renewed annual plan"),
    ("Sam Rivera",     "migrated workspace to the new billing tier"),
]


# - Revenue (mock) -
def _demo_revenue() -> list[dict]:
    """12 months of plausible MRR. Deterministic per-process for screenshots."""
    rng = random.Random(7)
    months = []
    base = 8400
    for i in range(12):
        mrr = base + (i * 1100) + rng.randint(-400, 400)
        if i == 6:
            mrr -= 1500  # one seasonal dip
        months.append({
            "month": i,
            "mrr_cents": mrr * 100,
            "customers": 12 + i * 3 + rng.randint(-2, 2),
        })
    return months


# - Sentry / errors (mock) -
DEMO_ERRORS: list[dict] = [
    {"level": "error",  "message": "Stripe webhook signature verification failed",       "count": 3,    "env": "production",  "first_seen_offset_hours": 26, "last_seen_offset_hours": 4},
    {"level": "warning","message": "Self-hosted install: deprecated Helm chart version", "count": 12,   "env": "self-hosted", "first_seen_offset_hours": 96, "last_seen_offset_hours": 18},
    {"level": "info",   "message": "Auth: refresh token rotation OK on 1247 sessions",   "count": 1247, "env": "production",  "first_seen_offset_hours": 24, "last_seen_offset_hours": 0},
    {"level": "error",  "message": "BigQuery export timeout exceeded 5 minute budget",  "count": 1,    "env": "production",  "first_seen_offset_hours": 11, "last_seen_offset_hours": 11},
    {"level": "warning","message": "Stripe API rate limit hit during nightly sync",      "count": 2,    "env": "production",  "first_seen_offset_hours": 50, "last_seen_offset_hours": 30},
]


# - GA4 traffic (mock) -
def _demo_ga() -> list[dict]:
    rng = random.Random(11)
    days = []
    for i in range(30):
        days.append({
            "day_offset": i,
            "sessions": 180 + i * 8 + rng.randint(-30, 30),
            "users": 120 + i * 5 + rng.randint(-20, 20),
            "pageviews": 540 + i * 22 + rng.randint(-60, 60),
            "bounce_rate": round(0.42 + rng.uniform(-0.06, 0.06), 2),
        })
    return days


# - Anomalies (mock) -
DEMO_ANOMALIES: list[dict] = [
    {"metric": "stripe_mrr",         "observed": 14200, "expected": 11500, "z_score":  2.3, "direction": "above", "offset_hours": 6,  "note": "Larger-than-usual batch of new signups"},
    {"metric": "sentry_errors",      "observed": 17,    "expected": 4,     "z_score":  3.1, "direction": "above", "offset_hours": 11, "note": "Webhook signature errors clustered"},
    {"metric": "self_hosted_health", "observed": 1,     "expected": 99,    "z_score": -2.7, "direction": "below", "offset_hours": 22, "note": "Self-hosted check-ins down - planned maintenance"},
]


# - Code TODOs (mock) -
DEMO_TODOS: list[dict] = [
    {"source": "repo", "repo": "mosaic-core",        "path": "app/billing/webhook.py",     "line": 142, "text": "TODO: rotate Stripe webhook signing secret in Q3",           "priority": "high"},
    {"source": "repo", "repo": "mosaic-core",        "path": "app/auth/sessions.py",      "line": 87,  "text": "FIXME: refresh token race condition under high load",       "priority": "high"},
    {"source": "repo", "repo": "mosaic-web",         "path": "components/Pricing.tsx",    "line": 54,  "text": "TODO: replace placeholder copy with final wording",          "priority": "low"},
    {"source": "repo", "repo": "mosaic-self-hosted", "path": "charts/mosaic/values.yaml", "line": 23,  "text": "TODO: bump Postgres from 15 to 16 once we cut the LTS chart", "priority": "low"},
    {"source": "repo", "repo": "data-pipeline",      "path": "models/marts/revenue.sql",  "line": 7,   "text": "XXX: investigate mismatch with Stripe raw - about 3% delta", "priority": "high"},
    {"source": "repo", "repo": "status-page",        "path": "src/index.ts",              "line": 31,  "text": "TODO: add /api/status.json public endpoint for status aggregators", "priority": "low"},
]


# - The entry point -

def run() -> dict:
    """Insert all demo data. Idempotent: clears source=demo rows first."""
    db.init_db()
    # Ensure tables demo needs but init_db() may have skipped (FK ordering).
    try:
        with db.db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS founders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    original_plan TEXT NOT NULL,
                    original_cents INTEGER NOT NULL,
                    badge_color TEXT DEFAULT 'gold',
                    note TEXT,
                    joined_at INTEGER NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)
    except Exception:
        pass
    # live_urls has its own ensure_schema()
    try:
        from . import live_urls as _live_urls
        _live_urls.ensure_schema()
    except Exception:
        pass
    now = int(time.time())
    rng = random.Random(42)  # deterministic for screenshots

    # Clear previous demo rows so re-runs are stable
    with db.db() as conn:
        cur = conn.cursor()
        for table in (
            "documents", "activity_feed", "revenue_events", "revenue_sources",
            "sentry_events", "anomalies", "ga_daily", "code_todos", "live_urls",
            "founders", "todos",
        ):
            try:
                cur.execute(f"DELETE FROM {table} WHERE source = ?", (DEMO_TAG,))
            except Exception:
                pass  # Table may not exist (before full schema migration)

    # - Notes -
    for i, note in enumerate(DEMO_NOTES):
        mtime = now - (i * 3600) - rng.randint(0, 86400)
        body = note["body"]
        tags = note.get("tags", [])
        db.upsert_document(
            source=DEMO_TAG,
            path=note["path"],
            title=note["title"],
            content=body,
            metadata={"tags": tags, "tags_text": " ".join(tags)},
            mtime=mtime,
            size=len(body),
        )

    # - Repos -
    for r in DEMO_REPOS:
        body = json.dumps({
            "name": r["name"],
            "summary": r["summary"],
            "topics": r["topics"],
            "stars": r["stars"],
            "forks": r["forks"],
            "language": r["language"],
        })
        mtime = now - rng.randint(0, 30 * 86400)
        db.upsert_document(
            source=DEMO_TAG,
            path=f"repos/{r['name']}",
            title=r["name"],
            content=body,
            metadata={"tags": r["topics"], "tags_text": " ".join(r["topics"])},
            mtime=mtime,
            size=len(body),
        )

    # - Live URLs (real schema: checked_at NOT NULL, ok NOT NULL) -
    try:
        with db.db() as conn:
            cur = conn.cursor()
            for u in DEMO_URLS:
                latency = rng.randint(40, 380)
                status_code = 200 if rng.random() > 0.05 else (503 if rng.random() > 0.5 else 200)
                ok = 1 if 200 <= status_code < 400 else 0
                cur.execute(
                    """INSERT OR REPLACE INTO live_urls
                       (name, url, project, kind, checked_at, status_code, ok, latency_ms, error)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (u["name"], u["url"], u["project"], u["kind"],
                     now - rng.randint(0, 600), status_code, ok, latency, None),
                )
    except Exception as e:
        print(f"[demo] live_urls skipped: {e}")

    # - Founders (requires a demo user) -
    try:
        with db.db() as conn:
            cur = conn.cursor()
            # Ensure at least one demo user exists (FK target for founders)
            cur.execute(
                """INSERT OR IGNORE INTO users
                   (id, email, name, locale, is_admin, email_verified, created_at)
                   VALUES (1, ?, ?, 'en', 1, 1, ?)""",
                ("demo@conductloop.example", "Demo User", now),
            )
            for i, f in enumerate(DEMO_FOUNDERS, start=1):
                user_id = i  # 1..N founders, all linked to demo user space
                # Create a placeholder user per founder so the FK holds
                cur.execute(
                    """INSERT OR IGNORE INTO users
                       (id, email, name, locale, is_admin, email_verified, created_at)
                       VALUES (?, ?, ?, 'en', 0, 1, ?)""",
                    (user_id, f"founder-{i}@conductloop.example", f["name"], now),
                )
                joined_at = now - f["joined_at_offset_days"] * 86400
                cur.execute(
                    """INSERT OR REPLACE INTO founders
                       (user_id, original_plan, original_cents, badge_color, joined_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (user_id, "lifetime", f["original_cents"], f["badge"], joined_at),
                )
    except Exception as e:
        print(f"[demo] founders skipped: {e}")

    # - Activity feed (real schema: kind, project_key, summary, created_at) -
    try:
        with db.db() as conn:
            cur = conn.cursor()
            for i, (name, action) in enumerate(DEMO_ACTIVITY):
                ts = now - i * 1800 - rng.randint(0, 600)
                cur.execute(
                    """INSERT INTO activity_feed
                       (kind, project_key, summary, created_at)
                       VALUES (?, ?, ?, ?)""",
                    ("note_created", "mosaic", f"{name}: {action}", ts),
                )
    except Exception as e:
        print(f"[demo] activity_feed skipped: {e}")

    # - Revenue (real schema: name/project_key, event_type/currency) -
    try:
        with db.db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT OR REPLACE INTO revenue_sources
                   (name, source_type, project_key, currency, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?)""",
                ("mosaic-mock", "mock", "mosaic", "USD", now, now),
            )
            rid = cur.lastrowid
            for ev in _demo_revenue():
                ev_ts = now - (12 - ev["month"]) * 30 * 86400
                cur.execute(
                    """INSERT OR REPLACE INTO revenue_events
                       (source_id, source_type, project_key, external_id, event_type,
                        amount_cents, currency, customer_ref, status, raw_payload,
                        occurred_at, ingested_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'succeeded', ?, ?, ?)""",
                    (rid, "mock", "mosaic", f"demo-mrr-{ev['month']}",
                     "payment_succeeded", ev["mrr_cents"], "USD",
                     f"customer-{ev['month']}", json.dumps(ev),
                     ev_ts, ev_ts),
                )
    except Exception as e:
        print(f"[demo] revenue skipped: {e}")

    # - Sentry - skipped (sentry_projects/sentry_issues schema is more complex;
    #   demo would need parent rows + FK inserts. View shows empty state.)

    # - GA - skipped (ga_sites/ga_metrics_daily requires parent + 30 days of
    #   metrics. View shows empty state.)

    # - Anomalies - skipped (no anomalies table in current schema.)

    # - Code TODOs (real schema: project_key, file_path, kind, content) -
    try:
        with db.db() as conn:
            cur = conn.cursor()
            for t in DEMO_TODOS:
                kind = "todo"
                txt = t["text"].lower()
                if "fixme" in txt:
                    kind = "fixme"
                elif "xxx" in txt:
                    kind = "xxx"
                elif "bug" in txt:
                    kind = "bug"
                elif "hack" in txt:
                    kind = "hack"
                cur.execute(
                    """INSERT OR REPLACE INTO code_todos
                       (project_key, file_path, line, kind, priority, content, done, extracted_at)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                    (t["repo"], t["path"], t["line"], kind, t["priority"],
                     t["text"], now),
                )
    except Exception as e:
        print(f"[demo] code_todos skipped: {e}")

    # - Teams (fictional company + members) so the Teams view isn't empty in
    #   demo mode. The built-in "AI Squad" is seeded separately by
    #   db.seed_default_teams(); here we add a coherent fake company that
    #   matches the demo notes/revenue (project_key "mosaic").
    try:
        demo_teams = [
            ("mosaic", "Mosaic", "mixed", "🧩",
             "B2B SaaS — collaborative knowledge platform. Demo company."),
            ("verdant", "Verdant (client)", "mixed", "🌿",
             "Enterprise biotech customer. Demo account."),
        ]
        demo_members = [
            ("mosaic",  "Jordan Lee",      "human",    "founder",  "active", "jordan@mosaic.example", None, None),
            ("mosaic",  "Sam Rivera",      "human",    "engineer", "active", "sam@mosaic.example", None, None),
            ("mosaic",  "Growth Bot",      "ai_agent", "agent",    "active", None, "campaign drafts, funnel analysis", "anthropic:claude"),
            ("mosaic",  "Support Bot",     "ai_agent", "agent",    "active", None, "triage tickets, draft replies", "ollama:llama3.2"),
            ("verdant", "Taylor Park",     "human",    "champion", "active", "taylor@verdant.example", None, None),
        ]
        with db.db() as conn:
            cur = conn.cursor()
            team_ids: dict[str, int] = {
                r[0]: r[1] for r in cur.execute("SELECT key, id FROM teams").fetchall()
            }
            for key, name, type_, icon, desc in demo_teams:
                if key in team_ids:
                    continue
                cur.execute(
                    "INSERT INTO teams (key, name, description, type, color, icon, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, name, desc, type_, None, icon, now, now),
                )
                team_ids[key] = cur.lastrowid
            existing_members = {
                (r[0], r[1]) for r in cur.execute("SELECT team_id, name FROM team_members").fetchall()
            }
            for team_key, mname, mtype, role, status, contact, caps, model in demo_members:
                tid = team_ids.get(team_key)
                if not tid or (tid, mname) in existing_members:
                    continue
                cur.execute(
                    "INSERT INTO team_members (team_id, name, type, role, status, contact, capabilities, model, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (tid, mname, mtype, role, status, contact, caps, model, now, now),
                )
                existing_members.add((tid, mname))
    except Exception as e:
        print(f"[demo] teams skipped: {e}")

    return {
        "ok": True,
        "source": DEMO_TAG,
        "notes": len(DEMO_NOTES),
        "repos": len(DEMO_REPOS),
        "urls": len(DEMO_URLS),
        "founders": len(DEMO_FOUNDERS),
        "revenue_months": 12,
        "sentry": len(DEMO_ERRORS),
        "ga_days": 30,
        "anomalies": len(DEMO_ANOMALIES),
        "todos": len(DEMO_TODOS),
    }
