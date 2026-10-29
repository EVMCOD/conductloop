"""The Linux bundles workflow (.github/workflows/linux-bundles.yml).

It builds the AppImage and .deb and uploads them as short-lived artifacts. It
must not publish anything, take a secret, or run anywhere but a GitHub-hosted
runner. (tests/test_workflows_guard.py checks pinning, permissions and runner
labels for every workflow; these are the checks specific to this one.)
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / ".github" / "workflows" / "linux-bundles.yml"
TEXT = PATH.read_text(encoding="utf-8")
DOC = yaml.safe_load(TEXT)
JOB = DOC["jobs"]["linux-bundles"]
TRIGGERS = DOC.get(True) or DOC.get("on")  # PyYAML reads the key `on` as True


def test_it_runs_on_ubuntu_22_04_for_an_older_glibc_and_nowhere_else():
    assert list(DOC["jobs"]) == ["linux-bundles"]
    assert JOB["runs-on"] == "ubuntu-22.04"  # older glibc: the AppImage runs on older distributions
    assert JOB["timeout-minutes"] <= 90


def test_triggers_are_manual_and_pull_requests_that_touch_the_desktop_app_or_the_script():
    assert set(TRIGGERS) == {"workflow_dispatch", "pull_request"}
    paths = TRIGGERS["pull_request"]["paths"]
    assert "desktop/**" in paths and "scripts/build-linux.sh" in paths
    # Not on push: bundles of main are not wanted on every merge.
    assert "push" not in TRIGGERS and "schedule" not in TRIGGERS


def test_it_builds_with_the_script_and_installs_webkitgtk_4_1():
    steps = "\n".join(str(s.get("run", "")) for s in JOB["steps"])
    assert "scripts/build-linux.sh" in steps
    assert "libwebkit2gtk-4.1-dev" in steps and "libgtk-3-dev" in steps


def test_both_bundles_are_uploaded_for_three_days():
    uploads = [s for s in JOB["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(uploads) == 2
    for step in uploads:
        assert step["with"]["retention-days"] == 3
        assert step["with"]["if-no-files-found"] == "error"
    paths = " ".join(s["with"]["path"] for s in uploads)
    assert ".AppImage" in paths and ".deb" in paths


def test_it_publishes_nothing_and_takes_no_secret():
    code = "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in TEXT.splitlines())
    lowered = code.lower()
    for word in ("secrets.", "gh release", "gh api", "action-gh-release", "git push", "git tag",
                 "tauri_signing", "contents: write", "id-token", "self-hosted"):
        assert word not in lowered, word
    assert DOC["permissions"] == {"contents": "read"}


def test_the_script_it_calls_exists_and_is_executable():
    script = ROOT / "scripts" / "build-linux.sh"
    assert script.is_file() and script.stat().st_mode & 0o111
