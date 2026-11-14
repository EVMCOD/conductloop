"""Risk overlay for AI workflows.

Conducts AI sells \"structured risk overlay covering MIT V4 risk taxonomy,
EU AI Act Article 22, GDPR, SOC 2\". For ConductLoop (personal/team tool),
this is overkill, but a lightweight checklist that flags common concerns is
useful as a built-in safety net.

Approach: a static rule-based scanner that looks at user-defined
\"AI workflows\" (saved as playbook entries with an action_type) and
generates a per-workflow risk report based on:
- Does the AI send data externally? (high risk for personal data)
- Does it make autonomous decisions? (EU AI Act Art 22 concerns)
- Does it touch financial systems? (SOC 2)
- Does it write to production? (blast radius)
- Does it have a kill switch? (basic safety)

Output: {score, level: 'low'|'medium'|'high', findings: [...]}

CLI:
  python -m ops_dashboard.cli risk [--project <key>]
  python -m ops_dashboard.cli risk-add <name> <action_type> [--notes]
"""
from __future__ import annotations

from .. import db

# Heuristic risk rules. Order matters (most severe first).
RISK_RULES = [
    {
        "id": "autonomous_decision",
        "title": "Autonomous decision-making (EU AI Act Article 22)",
        "severity": "high",
        "triggers": lambda w: w.get("autonomous", False),
        "guidance": "Article 22 of the EU AI Act grants individuals the right to contest "
                    "decisions made solely by automated systems. Add human review for high-impact decisions.",
    },
    {
        "id": "writes_to_production",
        "title": "Writes to production systems",
        "severity": "high",
        "triggers": lambda w: w.get("writes_production", False),
        "guidance": "Production writes should have rollback plan + canary deploy + human approval gate.",
    },
    {
        "id": "touches_financial",
        "title": "Touches financial / payment systems",
        "severity": "high",
        "triggers": lambda w: w.get("touches_financial", False),
        "guidance": "SOC 2 + PCI-DSS controls apply. Always log full audit trail, encrypt at rest, "
                    "and use idempotency keys for any state-mutating operation.",
    },
    {
        "id": "sends_pii",
        "title": "May process personal data (GDPR Art 6/9)",
        "severity": "medium",
        "triggers": lambda w: w.get("sends_pii", False),
        "guidance": "Document the lawful basis (consent, contract, etc). Apply data minimization. "
                    "Don't log full payloads. Add retention policies.",
    },
    {
        "id": "external_api_calls",
        "title": "Makes external API calls",
        "severity": "medium",
        "triggers": lambda w: w.get("external_apis", False),
        "guidance": "Use API keys with the least privilege needed. Set per-key rate limits and spend caps.",
    },
    {
        "id": "no_killswitch",
        "title": "No documented kill switch",
        "severity": "medium",
        "triggers": lambda w: not w.get("has_killswitch", False),
        "guidance": "Every autonomous workflow should have a documented way to stop it. "
                    "For ConductLoop: disable the source, delete the cron, or set enabled=0.",
    },
    {
        "id": "low_test_coverage",
        "title": "No automated tests / regression checks",
        "severity": "low",
        "triggers": lambda w: not w.get("has_tests", False),
        "guidance": "Add at least one smoke test that runs daily. If it fails, alert.",
    },
]


def assess_workflow(workflow: dict) -> dict:
    """Score a single workflow. Returns {score, level, findings}."""
    findings = []
    score = 0
    sev_score = {"low": 1, "medium": 3, "high": 9}
    for rule in RISK_RULES:
        try:
            if rule["triggers"](workflow):
                findings.append({
                    "id": rule["id"],
                    "title": rule["title"],
                    "severity": rule["severity"],
                    "guidance": rule["guidance"],
                })
                score += sev_score[rule["severity"]]
        except Exception:
            pass
    if score >= 9:
        level = "high"
    elif score >= 3:
        level = "medium"
    elif score > 0:
        level = "low"
    else:
        level = "none"
    return {"score": score, "level": level, "findings": findings}


def run_assessment(*, project_key: str | None = None) -> dict:
    """Assess all workflows. Reads playbook_entries as workflow definitions.

    Returns {total, by_level: {...}, workflows: [{name, level, score, findings}]}
    """
    # For now, derive workflows from playbook entries (anything with an action_type set).
    workflows = []
    try:
        with db.db() as conn:
            rows = conn.execute(
                "SELECT id, content, entry_kind, project_key FROM playbook_entries "
                "ORDER BY project_key, entry_kind, id"
            ).fetchall()
        for row in rows:
            content = row["content"] or ""
            if not any(k in content.lower() for k in ["action_type:", "autonomous:", "sends_pii:", "external_apis:"]):
                continue
            wf = _parse_workflow(content)
            wf["id"] = row["id"]
            wf["name"] = (row.get("entry_kind") or "workflow") + " #" + str(row["id"])
            wf["phase_key"] = row.get("entry_kind") or ""
            wf["project_key"] = row["project_key"]
            if project_key and wf["project_key"] != project_key:
                continue
            assessment = assess_workflow(wf)
            workflows.append({
                "id": wf["id"],
                "name": wf["name"],
                "project_key": wf["project_key"],
                "phase": wf["phase_key"],
                **assessment,
            })
    except Exception as e:
        return {"error": str(e), "workflows": []}
    by_level = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for w in workflows:
        by_level[w["level"]] = by_level.get(w["level"], 0) + 1
    return {
        "total": len(workflows),
        "by_level": by_level,
        "workflows": sorted(workflows, key=lambda x: -x["score"]),
    }


def _parse_workflow(content: str) -> dict:
    """Parse a playbook entry into a workflow dict for risk scanning.

    Looks for keywords like 'autonomous: true', 'sends_pii: true', etc.
    """
    lc = content.lower()
    return {
        "autonomous": any(k in lc for k in ["autonomous: true", "fully autonomous", "no human review"]),
        "writes_production": any(k in lc for k in ["writes to production", "production deploy", "git push --force"]),
        "touches_financial": any(k in lc for k in ["stripe", "payment", "invoice", "refund", "subscription"]),
        "sends_pii": any(k in lc for k in ["email", "user data", "pii", "personal info", "customer data"]),
        "external_apis": any(k in lc for k in ["api.stripe.com", "api.openai", "api.anthropic", "sentry.io", "fetch(", "http"]),
        "has_killswitch": any(k in lc for k in ["killswitch", "kill switch", "disable", "enabled=false", "enabled=0"]),
        "has_tests": any(k in lc for k in ["test:", "tests:", "smoke test", "pytest"]),
    }
