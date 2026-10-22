"""Skills indexer.

Walks ``<vault>/skills/`` (``OPS_SKILLS_DIR``) and, only if the user turned
it on, ``~/.agents/skills`` and ``~/.claude/skills`` (read only), and parses
each SKILL.md with ``ops_dashboard.skills`` (agentskills.io spec) into the
`skills` table. Auto-categorizes by name keywords and first-line of
description.

Categories we use (auto-detected):
  - frontend   (web/UI/React/Vue/CSS/a11y/design)
  - backend    (API/DB/server/queue/cache/CRUD)
  - mobile     (iOS/Android/Flutter/Swift/Kotlin/React Native)
  - devops     (Docker/K8s/CI/deploy/infrastructure)
  - ai         (LLM/RAG/agent/prompt/embed/ML)
  - data       (analytics/ETL/warehouse/stream/BI)
  - security   (auth/encrypt/vuln/audit/compliance/pen-test)
  - testing    (QA/e2e/TDD/test/ci-test/coverage)
  - design     (brand/visual/typography/motion/illustration)
  - marketing  (SEO/growth/content/social/email/copy)
  - research   (literature/review/oracle/analysis)
  - finance    (billing/subscription/Stripe/treasury/trade)
  - video      (motion/manim/video/editing/animation)
  - devtools   (skill/meta/agent/eval/testing-skills)
  - other      (catch-all)
"""
from __future__ import annotations

import json
import os as _os
import time
from pathlib import Path

from .. import db

ECC_SKILLS_DIR = Path(_os.environ.get("OPS_SKILLS_DIR", str(db.vault_root() / "skills"))).expanduser()


# Keyword → category map. First match wins; order matters.
CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("ai", [
        "llm", "rag", "agent", "prompt", "embed", "mcp", "eval-harness",
        "agent-eval", "agent-harness", "agent-architecture", "agentic",
        "ai-first", "ai-regression", "foundation-model", "iterative-retrieval",
        "token-budget", "cost-aware-llm", "continuous-learning", "skill-",
    ]),
    ("frontend", [
        "frontend", "react", "vue", "angular", "svelte", "css", "tailwind",
        "design-system", "a11y", "accessibility", "ui-to-", "ui-demo", "vite",
        "web", "browser-qa", "manim", "motion-ui", "motion-foundations",
        "motion-patterns", "motion-advanced", "liquid-glass",
    ]),
    ("mobile", [
        "ios", "android", "swift", "swiftui", "kotlin", "flutter", "dart",
        "react-native", "compose-multiplatform", "capacitor", "expo",
        "windows-desktop", "macos",
    ]),
    ("backend", [
        "api-", "fastapi", "django", "flask", "express", "nestjs", "spring",
        "springboot", "laravel", "rails", "gin", "quarkus", "ktor",
        "bun-runtime", "nodejs-", "rust-", "python-", "java-", "csharp-",
        "fsharp-", "cpp-", "go-", "backend-patterns", "redis-", "postgres-",
        "mysql-", "clickhouse", "click-path", "mongo",
    ]),
    ("devops", [
        "docker", "kubernetes", "k8s", "ci-", "deploy", "ci-cd", "argocd",
        "terraform", "ansible", "helm", "prometheus", "grafana", "istio",
        "envoy", "cisco-ios", "netmiko", "network-", "wireguard", "vlan",
        "homelab", "pihole", "flox", "uncloud", "git-workflow", "github-ops",
        "jira",
    ]),
    ("data", [
        "data-", "etl", "warehouse", "kafka", "clickhouse", "data-scraper",
        "messages-ops", "recsys", "prediction-market", "scraping",
        "elasticsearch", "throughput", "knowledge-ops", "search-first",
        "content-hash", "regex-vs-llm",
    ]),
    ("security", [
        "auth", "oauth", "jwt", "encrypt", "vuln", "audit", "compliance",
        "hipaa", "gdpr", "soc2", "pen-test", "bounty", "security-",
        "safe", "guard", "rules-distill", "ck", "hookify", "gateguard",
    ]),
    ("testing", [
        "tdd", "test-", "e2e", "qa", "ci-test", "coverage", "playwright",
        "verification-loop", "verification", "browser-qa", "click-path",
        "healthcare-eval-harness",
    ]),
    ("design", [
        "design-", "brand-", "visual", "typography", "illustration",
        "remotion", "manim-video", "video-editing", "videodb",
        "ui-to-", "motion-", "make-interfaces-feel", "blueprint",
    ]),
    ("marketing", [
        "seo", "growth", "content-", "social-", "email-", "copy",
        "campaign", "lead-intelligence", "investor-", "marketing-",
        "outreach", "article-writing", "crosspost",
    ]),
    ("research", [
        "research", "literature", "review", "analysis", "scientific",
        "deep-research", "pubmed", "uspto", "scholar", "exa-search",
        "market-research", "discovery",
    ]),
    ("finance", [
        "billing", "subscription", "stripe", "payment", "treasury",
        "trade", "trading", "defi", "amm", "evm-", "token-", "invoice",
        "refund", "carrier-", "customs-", "ito-", "ito-trade", "energy-",
        "logistics", "inventory", "production-", "manufacturing",
    ]),
    ("video", [
        "remotion", "manim", "video-", "motion-advanced", "videodb",
        "blender", "animation",
    ]),
    ("devtools", [
        "skill-", "configure-ecc", "ecc-", "orch-", "dmux-", "ralphinho",
        "blueprint", "council", "team-", "messages-ops", "unified-",
        "workspace-", "openclaw", "nanoclaw", "claude-devfleet",
    ]),
]

SUBCATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("iOS", ["ios", "swift", "swiftui", "swift-"]),
    ("Android", ["android", "kotlin", "compose-multiplatform"]),
    ("Flutter", ["flutter", "dart"]),
    ("React", ["react", "nextjs", "vite"]),
    ("Vue", ["vue", "nuxt"]),
    ("Python", ["python", "fastapi", "django", "flask", "quarkus"]),
    ("Go", ["golang", "go-"]),
    ("Rust", ["rust"]),
    ("Java", ["java", "spring", "jpa", "kotlin-"]),
    ("PHP", ["php", "laravel"]),
    ("TypeScript", ["typescript", "nextjs", "nodejs", "bun-runtime"]),
    ("AI / LLM", ["llm", "rag", "agent", "prompt", "embed", "mcp", "agentic"]),
    ("Stripe", ["stripe"]),
    ("Stripe / Billing", ["billing", "subscription", "invoice"]),
    ("Crypto / DeFi", ["defi", "amm", "evm", "token"]),
    ("Trading", ["trading", "ito-trade", "defi-amm"]),
    ("Healthcare", ["healthcare", "hipaa", "phi"]),
    ("Trading Agent", ["llm-trading-agent"]),
    ("Research", ["research", "literature", "scholar", "scientific", "deep-research"]),
    ("Video", ["video", "remotion", "manim", "motion"]),
    ("Design", ["design", "brand", "typography", "illustration"]),
    ("Marketing", ["seo", "marketing", "growth", "email-", "social-", "crosspost", "content-"]),
    ("Networking", ["network", "homelab", "wireguard", "vlan", "cisco", "netmiko"]),
    ("Kubernetes", ["kubernetes", "k8s", "helm"]),
    ("Testing", ["tdd", "test-", "e2e", "qa", "verification"]),
    ("Postgres", ["postgres", "prisma", "clickhouse", "mysql", "redis"]),
    ("Security", ["security", "auth", "encrypt", "compliance", "guard", "safe"]),
    ("Memory / Persistence", ["swift-actor", "persistence", "memory"]),
    ("ECC", ["ecc-", "configure-ecc"]),
    ("OpenClaw", ["openclaw", "nanoclaw", "claude-devfleet"]),
    ("Workflow", ["workflow", "orch-", "dmux-", "ralphinho"]),
    ("Search", ["search", "exa", "discovery"]),
]


def _categorize(key: str, name: str, description: str) -> str:
    blob = (key + " " + name + " " + description).lower()
    for cat, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in blob:
                return cat
    return "other"


def _subcategorize(key: str, name: str) -> str | None:
    blob = (key + " " + name).lower()
    for sub, keywords in SUBCATEGORY_RULES:
        for kw in keywords:
            if kw in blob:
                return sub
    return None


def run(root: Path | None = None, *, include_external: bool | None = None) -> dict:
    """Rebuild the ``skills`` table (L5): real YAML frontmatter (PyYAML
    safe_load, D8), tolerant validation with stored diagnostics, and the
    opt-in read-only external dirs (D2) after the vault, which wins on name
    clashes. Skills with errors are listed but never enter the Brain catalog."""
    from ..skills import catalog

    vault = root or catalog.vault_dir()
    # activate() only loads vault skills that live under catalog.vault_dir();
    # skills indexed from another root are listed but never offered as loadable.
    custom_root = root is not None and Path(root).resolve() != Path(catalog.vault_dir()).resolve()
    counters = {"indexed": 0, "skipped": 0, "errors": 0, "invalid": 0}
    if include_external is None:
        include_external = catalog.external_dirs_enabled()
    groups = [catalog.discover(vault, "vault")]
    if include_external:
        groups += [catalog.discover(catalog.source_root(k), k) for k in catalog.EXTERNAL_SOURCES]
    records = catalog.merge(groups)
    if not vault.exists() and not records:
        print(f"skills: directory not found: {vault}")
    with db.db() as conn:
        # Wipe and re-fill each run (skills change slowly). User state lives in skill_state.
        conn.execute("DELETE FROM skills")
        now = int(time.time())
        for rec in records:
            try:
                display = rec.key.replace("-", " ").title()
                cat = _categorize(rec.key, display, rec.description)
                sub = _subcategorize(rec.key, display)
                st = Path(rec.path).stat()
                conn.execute(
                    """INSERT INTO skills (key, name, description, category, subcategory, origin, path, mtime,
                       content_len, indexed_at, source_kind, diagnostics, loadable)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (rec.key, display, rec.description, cat, sub, rec.origin, rec.path, int(st.st_mtime),
                     st.st_size, now, rec.source_kind, json.dumps(rec.diag_dicts(), ensure_ascii=False),
                     1 if rec.loadable and not (custom_root and rec.source_kind == "vault") else 0),
                )
                counters["indexed"] += 1
                if not rec.loadable:
                    counters["invalid"] += 1
            except Exception as e:
                counters["errors"] += 1
                print(f"  ! error {rec.dir_name}: {e}")
    # Summary
    with db.db() as conn:
        cats = conn.execute("SELECT category, COUNT(*) AS c FROM skills GROUP BY category ORDER BY c DESC").fetchall()
    print(f"skills: indexed {counters['indexed']} skills from {vault}"
          + (" (+ ~/.agents/skills, ~/.claude/skills)" if include_external else ""))
    for c in cats:
        print(f"  {c['category']:12} {c['c']:4}")
    return counters


def list_skills(category: str | None = None, subcategory: str | None = None, query: str | None = None) -> list[dict]:
    sql = "SELECT * FROM skills"
    clauses: list[str] = []
    params: list = []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if subcategory:
        clauses.append("subcategory = ?")
        params.append(subcategory)
    if query:
        clauses.append("(LOWER(name) LIKE ? OR LOWER(description) LIKE ? OR LOWER(key) LIKE ?)")
        like = f"%{query.lower()}%"
        params.extend([like, like, like])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY category, subcategory, name"
    with db.db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def skills_summary() -> dict:
    with db.db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM skills").fetchone()["c"]
        by_cat = conn.execute(
            "SELECT category, COUNT(*) AS c FROM skills GROUP BY category ORDER BY c DESC"
        ).fetchall()
        by_sub = conn.execute(
            "SELECT category, subcategory, COUNT(*) AS c FROM skills "
            "WHERE subcategory IS NOT NULL GROUP BY category, subcategory ORDER BY category, c DESC"
        ).fetchall()
    return {
        "total": total,
        "by_category": [dict(r) for r in by_cat],
        "by_subcategory": [dict(r) for r in by_sub],
    }
