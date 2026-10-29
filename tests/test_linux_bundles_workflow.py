"""The Linux bundles workflow (.github/workflows/linux-bundles.yml).

It builds the AppImage and .deb, uploads them as short-lived artifacts and
smoke-tests both (scripts/linux-smoke.sh) on the build image and on
ubuntu-latest. It must not publish anything, take a secret, or run anywhere but
a GitHub-hosted runner. (tests/test_workflows_guard.py checks pinning, permissions and runner
labels for every workflow; these are the checks specific to this one.)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / ".github" / "workflows" / "linux-bundles.yml"
TEXT = PATH.read_text(encoding="utf-8")
DOC = yaml.safe_load(TEXT)
JOB = DOC["jobs"]["linux-bundles"]
SMOKE_JOB = DOC["jobs"]["linux-smoke"]
SMOKE = ROOT / "scripts" / "linux-smoke.sh"
TRIGGERS = DOC.get(True) or DOC.get("on")  # PyYAML reads the key `on` as True


def test_it_runs_on_ubuntu_22_04_for_an_older_glibc_and_nowhere_else():
    assert list(DOC["jobs"]) == ["linux-bundles", "linux-smoke"]
    assert JOB["runs-on"] == "ubuntu-22.04"  # older glibc: the AppImage runs on older distributions
    assert JOB["timeout-minutes"] <= 90


def test_triggers_are_manual_and_pull_requests_that_touch_the_desktop_app_or_the_script():
    assert set(TRIGGERS) == {"workflow_dispatch", "pull_request"}
    paths = TRIGGERS["pull_request"]["paths"]
    assert "desktop/**" in paths and "scripts/build-linux.sh" in paths and "scripts/linux-smoke.sh" in paths
    # Not on push: bundles of main are not wanted on every merge.
    assert "push" not in TRIGGERS and "schedule" not in TRIGGERS


def test_it_builds_with_the_script_and_installs_webkitgtk_4_1():
    steps = "\n".join(str(s.get("run", "")) for s in JOB["steps"])
    assert "scripts/build-linux.sh" in steps
    assert "libwebkit2gtk-4.1-dev" in steps and "libgtk-3-dev" in steps


def _uploads(job):
    return [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@")]


def test_both_bundles_are_uploaded_for_three_days():
    bundles = [s for s in _uploads(JOB) if s["with"]["name"] in ("conductloop-linux-appimage", "conductloop-linux-deb")]
    assert len(bundles) == 2
    for step in bundles:
        assert step["with"]["if-no-files-found"] == "error"
    paths = " ".join(s["with"]["path"] for s in bundles)
    assert ".AppImage" in paths and ".deb" in paths
    for step in _uploads(JOB) + _uploads(SMOKE_JOB):
        assert step["with"]["retention-days"] == 3


def _runs(job):
    return [str(s.get("run", "")) for s in job["steps"]]


def test_both_bundles_are_smoke_tested_on_the_build_image_after_they_are_uploaded():
    names = [s.get("name", "") for s in JOB["steps"]]
    last_upload = max(i for i, s in enumerate(JOB["steps"])
                      if s.get("with", {}).get("name") in ("conductloop-linux-appimage", "conductloop-linux-deb"))
    smoke = [i for i, r in enumerate(_runs(JOB)) if "scripts/linux-smoke.sh" in r]
    assert len(smoke) == 2 and min(smoke) > last_upload, names
    runs = "\n".join(_runs(JOB))
    assert "linux-smoke.sh appimage" in runs and "linux-smoke.sh deb" in runs
    assert "xvfb" in runs and "x11-utils" in runs and "imagemagick" in runs


def test_and_again_on_ubuntu_latest_from_the_uploaded_artifacts():
    assert SMOKE_JOB["needs"] == "linux-bundles"
    assert SMOKE_JOB["runs-on"] == "ubuntu-latest"
    assert SMOKE_JOB["timeout-minutes"] <= 30
    downloads = [s["with"]["name"] for s in SMOKE_JOB["steps"]
                 if str(s.get("uses", "")).startswith("actions/download-artifact@")]
    assert downloads == ["conductloop-linux-appimage", "conductloop-linux-deb"]
    runs = "\n".join(_runs(SMOKE_JOB))
    assert "linux-smoke.sh appimage bundles/appimage/" in runs and "linux-smoke.sh deb bundles/deb/" in runs


def test_a_failed_check_still_runs_the_other_and_keeps_the_screenshots():
    for job in (JOB, SMOKE_JOB):
        steps = job["steps"]
        deb = next(s for s in steps if "linux-smoke.sh deb" in str(s.get("run", "")))
        assert deb.get("if") == "${{ !cancelled() }}"
        shots = [s for s in _uploads(job) if s["with"]["name"].startswith("conductloop-linux-smoke-")]
        assert len(shots) == 1 and shots[0].get("if") == "${{ !cancelled() }}"


def test_the_smoke_script_only_reads_the_local_server_and_keeps_to_a_temporary_home():
    code = "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in SMOKE.read_text(encoding="utf-8").splitlines())
    hosts = set(re.findall(r"https?://([^/\s\"']+)", code))
    assert hosts == {"127.0.0.1:7777"}, hosts
    for word in ("-X ", "--data", "-d ", "gh ", "git "):
        assert word not in code, word
    assert 'HOME="$TMP/home"' in code and "env -i" in code
    assert "--appimage-extract-and-run" in code  # no FUSE needed


@pytest.mark.posix_perms
def test_the_smoke_script_exists_and_is_executable():
    assert SMOKE.is_file() and SMOKE.stat().st_mode & 0o111


def test_it_publishes_nothing_and_takes_no_secret():
    code = "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in TEXT.splitlines())
    lowered = code.lower()
    for word in ("secrets.", "gh release", "gh api", "action-gh-release", "git push", "git tag",
                 "tauri_signing", "contents: write", "id-token", "self-hosted"):
        assert word not in lowered, word
    assert DOC["permissions"] == {"contents": "read"}


@pytest.mark.posix_perms
def test_the_script_it_calls_exists_and_is_executable():
    script = ROOT / "scripts" / "build-linux.sh"
    assert script.is_file() and script.stat().st_mode & 0o111
