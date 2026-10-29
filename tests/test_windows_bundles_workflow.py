"""The Windows bundles workflow (.github/workflows/windows-bundles.yml).

It builds the Windows installers and uploads them as short-lived artifacts. It
runs only when started by hand, must not publish anything, take a secret, or
run anywhere but a GitHub-hosted runner. (tests/test_workflows_guard.py checks
pinning, permissions and runner labels for every workflow; these are the checks
specific to this one.)
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / ".github" / "workflows" / "windows-bundles.yml"
TEXT = PATH.read_text(encoding="utf-8")
DOC = yaml.safe_load(TEXT)
JOB = DOC["jobs"]["windows-bundles"]
TRIGGERS = DOC.get(True) or DOC.get("on")  # PyYAML reads the key `on` as True


def test_it_runs_on_windows_latest_and_nowhere_else():
    assert list(DOC["jobs"]) == ["windows-bundles"]
    assert JOB["runs-on"] == "windows-latest"
    assert JOB["timeout-minutes"] <= 90


def test_it_runs_only_when_started_by_hand():
    # Not on push, pull_request or a schedule: it is not a check of any change.
    assert set(TRIGGERS) == {"workflow_dispatch"}


def test_it_is_not_a_required_check():
    """Required checks live in branch protection, not in the repo; the names a
    job reports must not collide with the ones that are required there."""
    assert set(DOC["jobs"]).isdisjoint({"test", "gitleaks"})
    assert JOB.get("name", "") not in {"test", "gitleaks"}


def test_it_builds_with_the_script_on_windows():
    steps = "\n".join(str(s.get("run", "")) for s in JOB["steps"])
    assert "scripts\\build-windows.ps1" in steps
    assert JOB["defaults"]["run"]["shell"] == "pwsh"
    uses = " ".join(str(s.get("uses", "")) for s in JOB["steps"])
    for action in ("actions/checkout@", "actions/setup-python@", "dtolnay/rust-toolchain@", "Swatinem/rust-cache@"):
        assert action in uses


def test_both_installers_are_uploaded_for_three_days():
    uploads = [s for s in JOB["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(uploads) == 2
    for step in uploads:
        assert step["with"]["retention-days"] == 3
        assert step["with"]["if-no-files-found"] == "error"
    paths = " ".join(s["with"]["path"] for s in uploads)
    assert "bundle/nsis/*.exe" in paths and "bundle/msi/*.msi" in paths


def test_it_publishes_nothing_and_takes_no_secret():
    code = "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in TEXT.splitlines())
    lowered = code.lower()
    for word in ("secrets.", "gh release", "gh api", "action-gh-release", "git push", "git tag",
                 "tauri_signing", "contents: write", "id-token", "self-hosted", "vars.", "signtool"):
        assert word not in lowered, word
    assert DOC["permissions"] == {"contents": "read"}
    for step in JOB["steps"]:
        assert "env" not in step or not any("secret" in str(v).lower() for v in step["env"].values())


def test_the_installed_app_smoke_test_cannot_turn_the_build_red():
    # A runner has no real desktop session: that step is informational.
    smoke = [s for s in JOB["steps"] if "install and start" in s.get("name", "")]
    assert len(smoke) == 1 and smoke[0].get("continue-on-error") is True
    # The frozen-server check is not optional: it needs no desktop session.
    server = [s for s in JOB["steps"] if "frozen server" in s.get("name", "")]
    assert len(server) == 1 and not server[0].get("continue-on-error")


def test_the_script_it_calls_exists():
    assert (ROOT / "scripts" / "build-windows.ps1").is_file()


def test_the_docs_mention_the_workflow():
    doc = (ROOT / "docs" / "PORTABILITY.md").read_text(encoding="utf-8")
    assert "windows-bundles.yml" in doc
    assert "workflow_dispatch" in doc[doc.index("windows-bundles.yml") - 600:doc.index("windows-bundles.yml") + 600]
