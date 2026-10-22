"""Pre-configured Boardroom starter pack.

When a fresh install opens the Boardroom for the first time, they see an
empty inbox. This module ships a curated set of 12 AI CEOs that cover the
core functions of a solo founder / small operator:

  1.  CSO             (strategy, OKRs, market intel)
  2.  CFO             (runway, burn, fundraising, metrics)
  3.  CTO             (engineering architecture, code review)
  4.  CMO             (marketing, SEO, content, growth)
  5.  VP Design       (UI/UX, design systems, branding)
  6.  VP Frontend     (React/Next/Tailwind, a11y, perf)
  7.  VP Mobile       (iOS/Android, App Store, Capacitor/SwiftUI)
  8.  VP DevOps       (CI/CD, infra, monitoring, cost)
  9.  Head of Product (roadmap, user research, prioritization)
  10. Head of Sales   (outreach, pipeline, conversion)
  11. General Counsel (contracts, IP, GDPR, terms)
  12. Head of CS      (onboarding, support, churn)

Each agent has:
  - avatar emoji + role + level + name
  - capabilities (free-text, used in prompts)
  - default tools (system functions they can call)
  - autonomy tier (1=autonomous, 2=notify, 3=approve, 4=escalate)
  - model preference (NULL = inherit user's BRAIN_PROVIDER)

Seeding is idempotent: re-running won't create duplicates.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

# ── Starter pack definition ────────────────────────────────────────────────────

STARTER_PACK: list[dict[str, Any]] = [
    {
        "name": "Avery (CSO)",
        "role": "cso",
        "title": "Chief Strategy Officer",
        "level": "ceo",
        "avatar": "🧭",
        "description": "Strategy, OKRs, market analysis, competitive intel, "
                       "long-term planning. Reads industry reports and tells you "
                       "what to focus on next.",
        "capabilities": "strategy; okrs; market-research; competitive-analysis; prioritization",
        "tools": ["projects", "playbooks", "decision", "search", "digest"],
        "autonomy_tier": 3,  # approve
        "model": None,
    },
    {
        "name": "Penelope (CFO)",
        "role": "cfo",
        "title": "Chief Financial Officer",
        "level": "ceo",
        "avatar": "💰",
        "description": "Tracks burn rate, runway, MRR/ARR, fundraising rounds, "
                       "unit economics. Surfaces the next dollar question before "
                       "you have to ask.",
        "capabilities": "finance; runway; mrr; arr; fundraising; unit-economics; pricing",
        "tools": ["projects", "cost", "decision"],
        "autonomy_tier": 3,  # approve
        "model": None,
    },
    {
        "name": "Atlas (CTO)",
        "role": "cto",
        "title": "Chief Technology Officer",
        "level": "ceo",
        "avatar": "🛠️",
        "description": "Engineering architecture, code review, technical debt "
                       "triage, build vs buy, vendor selection. Your second opinion "
                       "before any non-trivial code change.",
        "capabilities": "architecture; code-review; devops; technical-debt; vendor-selection",
        "tools": ["projects", "code_todos", "decision", "github"],
        "autonomy_tier": 2,  # notify
        "model": None,
    },
    {
        "name": "Mira (CMO)",
        "role": "cmo",
        "title": "Chief Marketing Officer",
        "level": "ceo",
        "avatar": "📣",
        "description": "Positioning, messaging, SEO content briefs, social strategy, "
                       "launch playbooks, AEO (answer-engine optimization). "
                       "Drafts blog posts and ad copy.",
        "capabilities": "marketing; seo; aeo; content; copywriting; positioning; launch",
        "tools": ["projects", "urls", "decision", "playbooks"],
        "autonomy_tier": 2,  # notify
        "model": None,
    },
    {
        "name": "Nora (VP Design)",
        "role": "vp_design",
        "title": "VP of Design",
        "level": "vp",
        "avatar": "🎨",
        "description": "UI/UX patterns, design systems, accessibility, brand "
                       "consistency. Reviews your designs before you ship and "
                       "calls out every 4px off-grid moment.",
        "capabilities": "ui; ux; design-systems; accessibility; branding; prototyping",
        "tools": ["projects", "playbooks"],
        "autonomy_tier": 2,
        "model": None,
    },
    {
        "name": "Felix (VP Frontend)",
        "role": "vp_frontend",
        "title": "VP of Frontend Engineering",
        "level": "vp",
        "avatar": "⚛️",
        "description": "React/Next.js/Vue/Tailwind/CSS, browser quirks, "
                       "performance budgets, accessibility, SEO meta. The senior "
                       "dev you call when the layout breaks at 320px.",
        "capabilities": "react; nextjs; typescript; tailwind; css; a11y; web-perf; browser-apis",
        "tools": ["projects", "code_todos", "decision"],
        "autonomy_tier": 2,
        "model": None,
    },
    {
        "name": "Ada (VP Mobile)",
        "role": "vp_mobile",
        "title": "VP of Mobile Engineering",
        "level": "vp",
        "avatar": "📱",
        "description": "iOS (SwiftUI/UIKit), Android (Kotlin/Compose), React Native, "
                       "Capacitor, app store submission, IAP, push notifications. "
                       "Owns your mobile roadmap end-to-end.",
        "capabilities": "swift; swiftui; ios; kotlin; android; capacitor; app-store; iap",
        "tools": ["projects", "code_todos", "decision"],
        "autonomy_tier": 2,
        "model": None,
    },
    {
        "name": "Orion (VP DevOps)",
        "role": "vp_devops",
        "title": "VP of DevOps / SRE",
        "level": "vp",
        "avatar": "☁️",
        "description": "Cloudflare, AWS, GCP, CI/CD, monitoring, cost optimization, "
                       "incident response, SLOs. Catches the $4,000 Cloud Run "
                       "invoice before it arrives.",
        "capabilities": "cloudflare; aws; gcp; ci-cd; monitoring; sre; cost-optimization",
        "tools": ["projects", "cost", "decision"],
        "autonomy_tier": 2,
        "model": None,
    },
    {
        "name": "Priya (Head of Product)",
        "role": "head_product",
        "title": "Head of Product",
        "level": "director",
        "avatar": "🗺️",
        "description": "Roadmap prioritization, user research synthesis, PRDs, "
                       "RICE scoring, OKR tracking. Tells you what NOT to build.",
        "capabilities": "product; roadmap; user-research; prioritization; prd; rice; jobs-to-be-done",
        "tools": ["projects", "decision", "playbooks"],
        "autonomy_tier": 2,
        "model": None,
    },
    {
        "name": "Diego (Head of Sales)",
        "role": "head_sales",
        "title": "Head of Sales",
        "level": "director",
        "avatar": "🤝",
        "description": "Outreach sequences, cold email, pipeline management, "
                       "qualification frameworks, demo scripts, follow-ups. "
                       "Turns cold leads into pipeline.",
        "capabilities": "sales; outreach; cold-email; pipeline; qualification; demos; follow-ups",
        "tools": ["projects", "decision", "playbooks"],
        "autonomy_tier": 2,
        "model": None,
    },
    {
        "name": "Helena (General Counsel)",
        "role": "general_counsel",
        "title": "General Counsel",
        "level": "director",
        "avatar": "⚖️",
        "description": "Contracts review, GDPR/CCPA/LGPD compliance, IP, terms of "
                       "service, privacy policy, data processing agreements. The "
                       "lawyer you can't afford (yet).",
        "capabilities": "legal; contracts; gdpr; ccpa; ip; terms; privacy; dpa",
        "tools": ["projects", "decision", "playbooks"],
        "autonomy_tier": 3,  # approve
        "model": None,
    },
    {
        "name": "Sofia (Head of CS)",
        "role": "head_cs",
        "title": "Head of Customer Success",
        "level": "director",
        "avatar": "💬",
        "description": "Onboarding flows, support macros, churn signals, NPS "
                       "analysis, expansion plays. The voice of the customer, "
                       "always on call.",
        "capabilities": "customer-success; onboarding; support; churn; nps; expansion",
        "tools": ["projects", "decision", "playbooks"],
        "autonomy_tier": 2,
        "model": None,
    },
]


HOLDING_TEAM_KEY = "holding"


def _holding_team_id(conn: sqlite3.Connection, now: int | str) -> int:
    """First team in the DB, or a "Holding" team created for the starter pack.

    teams.key is UNIQUE NOT NULL, so a fresh install (no teams yet) needs an
    explicit key; INSERT OR IGNORE + lookup keeps it idempotent.
    """
    row = conn.execute("SELECT id FROM teams ORDER BY id LIMIT 1").fetchone()
    if row:
        return int(row[0])
    conn.execute(
        "INSERT OR IGNORE INTO teams (key, name, type, icon, created_at, updated_at) "
        "VALUES (?, 'Holding', 'ai_squad', '🏛️', ?, ?)",
        (HOLDING_TEAM_KEY, now, now),
    )
    return int(conn.execute("SELECT id FROM teams WHERE key = ?", (HOLDING_TEAM_KEY,)).fetchone()[0])


def seed_starter_pack(conn: sqlite3.Connection, *, chairman_user_id: int, dry_run: bool = False) -> dict:
    """Insert the starter pack if Boardroom is empty.

    Idempotent: if there's already at least one CEO, no-op.
    Returns a summary dict with created/skipped counts and IDs.
    """
    existing = conn.execute(
        "SELECT COUNT(*) FROM team_members WHERE type = 'ai_agent' AND level IN ('ceo','vp','director')"
    ).fetchone()[0]

    summary = {
        "ran": True,
        "pre_existing_ceos": existing,
        "created": [],
        "skipped_existing": [],
        "dry_run": dry_run,
    }

    # Note: we don't early-return on existing CEOs. The per-name check below
    # makes this idempotent: re-running won't duplicate. That handles the case
    # of a dirty DB where some starter-pack CEOs already exist and others don't.

    now = conn.execute("SELECT strftime('%s','now')").fetchone()[0]

    for agent in STARTER_PACK:
        # Avoid duplicates by name
        already = conn.execute(
            "SELECT id FROM team_members WHERE name = ?", (agent["name"],)
        ).fetchone()
        if already:
            summary["skipped_existing"].append(agent["name"])
            continue

        if dry_run:
            summary["created"].append({"name": agent["name"], "dry_run": True})
            continue

        # Insert team_member
        team_id = _holding_team_id(conn, now)
        capabilities_json = json.dumps({
            "skills": agent["capabilities"],
            "description": agent["description"],
            "avatar": agent["avatar"],
            "tools": agent["tools"],
        }, ensure_ascii=False)

        cur = conn.execute(
            """INSERT INTO team_members
               (team_id, name, type, role, status, contact, capabilities, model,
                notes, workload, level, manager_id, created_at, updated_at)
               VALUES (?, ?, 'ai_agent', ?, 'active', NULL, ?, ?, ?, 0, ?, NULL, ?, ?)""",
            (team_id, agent["name"], agent["role"],
             capabilities_json, agent["model"], agent["title"],
             agent["level"], now, now),
        )
        ceo_id = cur.lastrowid
        # Mark as CEO (boardroom uses this flag)
        try:
            conn.execute(
                "UPDATE team_members SET is_ceo = 1 WHERE id = ?", (ceo_id,)
            )
        except sqlite3.OperationalError:
            # is_ceo column might not exist yet — schema drift; add it
            try:
                conn.execute("ALTER TABLE team_members ADD COLUMN is_ceo INTEGER NOT NULL DEFAULT 0")
                conn.execute("UPDATE team_members SET is_ceo = 1 WHERE id = ?", (ceo_id,))
            except sqlite3.OperationalError:
                pass

        # Default autonomy
        conn.execute(
            """INSERT OR REPLACE INTO autonomy_config
               (member_id, default_tier, maturity_stage, per_action, require_chairman, updated_at)
               VALUES (?, ?, 'traction', '{}', 0, ?)""",
            (ceo_id, agent["autonomy_tier"], now),
        )

        summary["created"].append({"name": agent["name"], "id": ceo_id})

    summary["action"] = "seeded" if not dry_run else "dry_run"
    return summary


def is_boardroom_empty(conn: sqlite3.Connection) -> bool:
    n = conn.execute(
        "SELECT COUNT(*) FROM team_members WHERE type = 'ai_agent' AND level IN ('ceo','vp','director')"
    ).fetchone()[0]
    return n == 0


# CLI entry: `python -m ops_dashboard seed-boardroom`
def main() -> int:
    import argparse

    from . import db as _db

    p = argparse.ArgumentParser(description="Seed the ConductLoop Boardroom with a starter pack of AI CEOs.")
    p.add_argument("--dry-run", action="store_true", help="Show what would be created without committing.")
    p.add_argument("--force", action="store_true", help="Seed even if Boardroom already has CEOs.")
    p.add_argument("--list", action="store_true", help="Just list the starter pack and exit.")
    args = p.parse_args()

    if args.list:
        print(f"Starter pack: {len(STARTER_PACK)} agents\n")
        for a in STARTER_PACK:
            print(f"  {a['avatar']}  {a['name']:24}  {a['title']}")
            print(f"      skills: {a['capabilities'][:80]}...")
            print(f"      autonomy tier: {a['autonomy_tier']}  tools: {', '.join(a['tools'])}")
        return 0

    db_path = _db.DB_PATH
    if not db_path.exists():
        print(f"❌ DB not found at {db_path}. Run 'python -m ops_dashboard index' first.")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    if args.force:
        # nuke existing CEOs except the Holding one (id 502 in current schema)
        conn.execute("""
            DELETE FROM team_members WHERE type='ai_agent'
            AND level IN ('ceo','vp','director')
            AND id NOT IN (SELECT id FROM team_members WHERE manager_id IS NULL)
        """)

    summary = seed_starter_pack(conn, chairman_user_id=0, dry_run=args.dry_run)

    if not args.dry_run:
        conn.commit()

    if args.dry_run:
        print(f"[DRY RUN] would create {len(summary['created'])} CEOs:")
    else:
        print(f"Action: {summary['action']}")
        print(f"Created {len(summary['created'])} CEOs:")
        for c in summary["created"]:
            if isinstance(c, dict) and "id" in c:
                print(f"  + {c['name']} (id={c['id']})")
            else:
                print(f"  + {c['name']}")

    if summary.get("skipped_existing"):
        print(f"Skipped (already exist): {', '.join(summary['skipped_existing'])}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
