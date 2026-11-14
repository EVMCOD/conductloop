"""The issue forms and the PR template must stay valid and in English.

GitHub silently ignores an issue form it cannot parse, so a typo would remove
the template without any CI failure. These checks keep them loadable.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

GITHUB = Path(__file__).resolve().parent.parent / ".github"
FORMS = sorted((GITHUB / "ISSUE_TEMPLATE").glob("*.yml"))
FORM_KEYS = {"name", "description", "body"}


def test_the_templates_exist():
    names = {p.name for p in FORMS}
    assert {"bug_report.yml", "feature_request.yml", "config.yml"} <= names
    assert (GITHUB / "pull_request_template.md").is_file()


@pytest.mark.parametrize("path", [p for p in FORMS if p.name != "config.yml"], ids=lambda p: p.name)
def test_issue_forms_are_valid(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert FORM_KEYS <= set(doc), f"{path.name} is missing {FORM_KEYS - set(doc)}"
    ids = []
    required = 0
    for item in doc["body"]:
        assert item["type"] in {"markdown", "textarea", "input", "dropdown", "checkboxes"}
        if item["type"] != "markdown":
            assert item.get("id"), f"{path.name}: every field needs an id"
            assert item["attributes"].get("label")
            ids.append(item["id"])
            required += bool(item.get("validations", {}).get("required"))
        if item["type"] == "dropdown":
            assert len(item["attributes"]["options"]) >= 2
    assert len(ids) == len(set(ids)), f"{path.name}: duplicate field ids"
    assert required, f"{path.name}: at least one field should be required"


def test_config_points_vulnerabilities_to_private_reporting():
    doc = yaml.safe_load((GITHUB / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8"))
    assert isinstance(doc["blank_issues_enabled"], bool)
    urls = [c["url"] for c in doc["contact_links"]]
    assert any(u.endswith("/security/advisories/new") for u in urls)
    assert all(u.startswith("https://") for u in urls)


def test_pr_template_has_the_required_checklist():
    text = (GITHUB / "pull_request_template.md").read_text(encoding="utf-8")
    for needle in ("scripts/verify.sh", "migration", "idempotent", "CHANGELOG.md", "personal data", "docs"):
        assert needle.lower() in text.lower(), f"PR template should mention {needle!r}"
    assert text.count("- [ ]") >= 6


def test_templates_are_english_and_ascii_only():
    for path in [*FORMS, GITHUB / "pull_request_template.md"]:
        text = path.read_text(encoding="utf-8")
        assert text.isascii(), f"{path.name}: non-ASCII text (templates are English-only)"
