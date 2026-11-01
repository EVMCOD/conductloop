"""Scan the external catalog for skills that fit the C-Suite roles.

Run with the project venv (PyYAML is required):
    .venv/bin/python scripts/scan_external_catalog.py <repo-skills-dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops_dashboard.skills import lint, parse, validate  # noqa: E402

ROLES = {
    "cfo": ["finance", "financial", "billing", "pricing", "revenue", "cost", "budget", "runway",
            "unit-economics", "accounting", "invoice", "payout", "profit", "cash-flow", "tax",
            "treasury", "subscription-billing", "payment"],
    "cmo": ["marketing", "seo", "content", "brand", "launch", "growth", "copy", "campaign",
            "positioning", "email-marketing", "newsletter", "social", "ads", "advertis", "keyword",
            "conversion-rate", "landing-page", "referral", "affiliate", "influencer"],
    "cso": ["strategy", "competitive", "market-research", "opportunity", "portfolio",
            "research-analyst", "moat", "benchmark", "swot"],
    "cto": ["code-review", "architecture", "refactor", "tech-debt", "technical-debt",
            "system-design", "api-design", "database", "schema", "performance", "security",
            "testing", "debug", "codebase", "api-versioning", "microservice", "typescript",
            "python", "react", "nextjs", "tailwind"],
    "general_counsel": ["legal", "contract", "privacy", "gdpr", "ccpa", "compliance",
                        "terms-of-service", "license", "policy", "hipaa", "soc2", "pci",
                        "intellectual-property", "trademark"],
    "head_cs": ["support", "onboarding", "churn", "retention", "customer-success", "nps",
                "helpdesk", "help-center", "ticket", "lifecycle"],
    "head_product": ["product", "roadmap", "prd", "discovery", "user-research", "backlog",
                     "priorit", "feature", "user-interview", "jobs-to-be-done"],
    "head_sales": ["sales", "lead-gen", "lead-generation", "outreach", "crm", "pipeline",
                   "demo", "negotiat", "cold-email", "prospect", "hubspot", "twenty-crm",
                   "follow-up"],
    "vp_design": ["design", "ux", "ui-", "figma", "prototype", "accessib", "design-system",
                  "a11y", "wireframe", "user-interface"],
    "vp_frontend": ["frontend", "react", "css", "tailwind", "nextjs", "next-js", "typescript",
                    "web-", "component", "vite", "storybook", "bundler"],
    "vp_mobile": ["ios", "android", "swift", "mobile", "app-store", "flutter", "react-native",
                  "capacitor", "expo", "swiftui", "kotlin"],
    "vp_devops": ["docker", "kubernetes", "k8s", "ci-cd", "cicd", "monitoring", "incident",
                  "sre", "infrastructure", "terraform", "deploy", "observability", "prometheus",
                  "grafana", "argocd", "github-actions", "container", "nginx"],
}


def main() -> None:
    root = Path(sys.argv[1])
    valid: dict[str, str] = {}
    total = 0
    for sk in sorted(root.rglob("SKILL.md")):
        total += 1
        raw = sk.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        parsed = parse.parse_skill_file(sk)
        if parsed.frontmatter is None:
            continue
        key, diags = validate.validate(parsed.frontmatter, sk.parent.name, parsed.body)
        if validate.has_errors(diags) or key != sk.parent.name:
            continue
        if [f for f in lint.scan_text(text, sk.parent.name) if f.level == "block"]:
            continue
        valid[sk.parent.name] = text

    print(f"skills en el repo: {total}")
    print(f"válidas y sin block: {len(valid)}\n")
    for role, kws in ROLES.items():
        hits = sorted(n for n in valid if any(k in n.lower() for k in kws))
        shown = ", ".join(hits[:14])
        more = " …" if len(hits) > 14 else ""
        print(f"{role:16} {len(hits):3}  {shown}{more}")


if __name__ == "__main__":
    main()
