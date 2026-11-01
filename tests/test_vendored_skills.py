"""The vendored third-party skills must stay attributable and non-duplicated.

A skill copied from another project is redistributed under that project's
license, so the copy has to say so: the license, who wrote it, and where it
came from. Without that the product ships someone else's work with no
attribution, and there is no way to trace it back when they update it.

The second rule is that a vendored skill must not collide with one of our own:
the catalog resolves a skill by its key, so two skills with one key means one
of them is unreachable.
"""
from __future__ import annotations

import json
from pathlib import Path

PACKS = Path(__file__).resolve().parent.parent / "ops_dashboard" / "skills" / "packs"
THIRD_PARTY = PACKS / "THIRD_PARTY.md"

# The skills this project wrote itself, by pack. A vendored skill may not
# reuse one of these names.
OWN_SKILLS = {
    "cfo": {"cash-runway-planning", "pricing-change-analysis", "unit-economics-review"},
    "cmo": {"content-brief", "launch-plan", "positioning-statement"},
    "cso": {"competitive-positioning", "opportunity-assessment", "portfolio-prioritization"},
    "cto": {"build-vs-buy-decision", "code-review-rigor", "technical-debt-triage"},
    "general_counsel": {"contract-review", "privacy-compliance-check",
                        "terms-and-policy-drafting"},
    "head_cs": {"churn-signal-triage", "onboarding-flow", "support-macro"},
    "head_product": {"discovery-interview", "prd-writing", "roadmap-prioritization"},
    "head_sales": {"lead-qualification", "outreach-sequence", "pipeline-hygiene"},
    "vp_design": {"accessibility-check", "design-review", "design-system-consistency"},
    "vp_devops": {"cost-audit", "deploy-verification", "incident-triage"},
    "vp_frontend": {"browser-quirks-triage", "component-contract-review",
                    "performance-budget"},
    "vp_mobile": {"app-store-submission", "mobile-release-safety",
                  "native-integration-review"},
}


def _packs() -> list[Path]:
    return sorted(p for p in PACKS.iterdir() if p.is_dir() and not p.name.startswith("."))


def test_every_pack_still_lists_exactly_what_is_on_disk():
    for pack in _packs():
        manifest = json.loads((pack / "pack.json").read_text(encoding="utf-8"))
        on_disk = sorted(p.name for p in pack.iterdir()
                         if p.is_dir() and not p.name.startswith("."))
        assert sorted(manifest["skills"]) == on_disk, pack.name


def test_the_third_party_notice_exists_and_names_the_source():
    assert THIRD_PARTY.is_file()
    text = THIRD_PARTY.read_text(encoding="utf-8")
    assert "TerminalSkills/skills" in text
    assert "Apache-2.0" in text


def test_every_vendored_skill_is_listed_in_the_third_party_notice():
    text = THIRD_PARTY.read_text(encoding="utf-8")
    for pack in _packs():
        for sk in sorted(pack.rglob("SKILL.md")):
            name = sk.parent.name
            if _is_ours(pack, name):
                continue
            assert f"`{name}`" in text, f"{pack.name}/{name} is not in THIRD_PARTY.md"


def _is_ours(pack: Path, name: str) -> bool:
    """A skill we wrote has no upstream source in its frontmatter."""
    from ops_dashboard.skills import parse
    sk = pack / name / "SKILL.md"
    if not sk.is_file():
        return False
    parsed = parse.parse_skill_file(sk)
    meta = (parsed.frontmatter or {}).get("metadata") or {}
    return not meta.get("source")


def test_every_vendored_skill_carries_its_license_and_source():
    from ops_dashboard.skills import parse
    seen = 0
    for pack in _packs():
        for sk in sorted(pack.rglob("SKILL.md")):
            parsed = parse.parse_skill_file(sk)
            fm = parsed.frontmatter or {}
            meta = fm.get("metadata") or {}
            if not meta.get("source"):
                continue
            seen += 1
            assert fm.get("license"), f"{sk} has no license"
            assert meta.get("author"), f"{sk} has no author"
            assert meta.get("source", "").startswith("https://"), sk
    assert seen, "no vendored skills found"


def test_no_vendored_skill_reuses_the_name_of_one_of_ours():
    for pack in _packs():
        own = OWN_SKILLS.get(pack.name, set())
        for name in own:
            sk = pack / name / "SKILL.md"
            if not sk.is_file():
                continue
            from ops_dashboard.skills import parse
            parsed = parse.parse_skill_file(sk)
            meta = (parsed.frontmatter or {}).get("metadata") or {}
            assert not meta.get("source"), (
                f"{pack.name}/{name} is one of ours but carries an upstream source")


def test_the_upstream_scoring_file_is_not_shipped():
    """_scores.json is the upstream project's own tooling, not part of the skill."""
    for pack in _packs():
        assert not list(pack.rglob("_scores.json")), pack.name
