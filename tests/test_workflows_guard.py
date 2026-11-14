"""Guards on the CI workflows of this PUBLIC repository.

* Nothing may run on a self-hosted runner. Anyone can open a pull request
  against a public repo, and a self-hosted runner would execute their code on
  a private machine. Runner labels must be literal GitHub-hosted ones: no
  ``self-hosted``, no custom label, no ``vars.RUNS_ON`` (a repository variable
  can be changed to point a job at any machine without a code review).
* Every third-party action is pinned by commit SHA, with the version it
  corresponds to in a comment, so a moved tag cannot change what CI runs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.y*ml"))
# The only runner labels a job may use. A pinned image (ubuntu-22.04, macos-14,
# ...) is not allowed, with ONE decision-backed exception: the Linux bundles job
# builds on ubuntu-22.04 so the AppImage works with an older glibc.
HOSTED_LABELS = frozenset({"ubuntu-latest", "macos-latest", "windows-latest"})
PINNED_IMAGE_EXCEPTIONS = {("linux-bundles.yml", "linux-bundles"): "ubuntu-22.04"}
# The only workflows that may build installers and keep them as artifacts. Both
# publish nothing and keep what they build for 3 days at most: a new workflow
# that uploads an artifact has to be added here on purpose, in review.
BUNDLE_WORKFLOWS = frozenset({"linux-bundles.yml", "windows-bundles.yml"})
FORBIDDEN = ("self-hosted", "vars.runs_on", "runs_on")
USES = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
PINNED = re.compile(r"^[\w.\-]+/[\w.\-]+(/[\w.\-/]+)?@[0-9a-f]{40}$")


def _code_lines(path: Path):
    """Lines of the workflow without full-line and trailing comments."""
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = re.sub(r"(^|\s)#.*$", "", line)
        if code.strip():
            yield n, code


def runner_problem(workflow: str, job: str, runs_on) -> str | None:
    """Why ``runs-on`` is not allowed for this job, or None when it is."""
    if not isinstance(runs_on, str):
        return "runs-on must be a plain label"
    if runs_on == PINNED_IMAGE_EXCEPTIONS.get((workflow, job)):
        return None
    if runs_on not in HOSTED_LABELS:
        return f"{runs_on!r} is not an allowed GitHub-hosted label ({', '.join(sorted(HOSTED_LABELS))})"
    return None


def test_there_are_workflows_to_check():
    assert WORKFLOWS, "no workflows found: the guard would pass on nothing"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_uses_a_self_hosted_runner(path):
    for n, code in _code_lines(path):
        lowered = code.lower()
        for word in FORBIDDEN:
            assert word not in lowered, f"{path.name}:{n}: {word!r} is not allowed in a public repo"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_runs_on_a_literal_github_hosted_runner(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = doc.get("jobs") or {}
    assert jobs, f"{path.name} has no jobs"
    for name, job in jobs.items():
        problem = runner_problem(path.name, name, job.get("runs-on"))
        assert problem is None, f"{path.name}:{name}: {problem}"
        assert job.get("timeout-minutes"), f"{path.name}:{name}: set timeout-minutes"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_by_sha_with_a_version_comment(path):
    seen = 0
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        m = USES.match(line)
        if not m or m.group("ref").startswith(("./", "docker://")):
            continue
        seen += 1
        assert PINNED.match(m.group("ref")), f"{path.name}:{n}: pin {m.group('ref')} to a commit SHA"
        comment = m.group("rest").strip()
        assert comment.startswith("#") and len(comment) > 2, (
            f"{path.name}:{n}: add the version as a comment, e.g. '# v1.2.3'")
    assert seen, f"{path.name} uses no actions: the check would pass on nothing"


def test_the_workflows_do_not_get_more_permissions_than_they_need():
    for path in WORKFLOWS:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert doc.get("permissions") == {"contents": "read"}, (
            f"{path.name}: set top-level 'permissions: contents: read'")


def test_the_desktop_wrapper_is_compiled_on_macos_only_when_desktop_changes():
    doc = yaml.safe_load((WORKFLOWS[0].parent / "desktop.yml").read_text(encoding="utf-8"))
    macos = [n for n, j in doc["jobs"].items() if j["runs-on"].startswith("macos-")]
    assert macos, "the Tauri wrapper must also be compiled on macOS"
    triggers = doc.get(True) or doc.get("on")  # PyYAML reads the key `on` as True
    for event in ("push", "pull_request"):
        assert triggers[event]["paths"] == ["desktop/**", ".github/workflows/desktop.yml"]


def test_ubuntu_22_04_is_allowed_only_for_the_linux_bundles_job():
    assert runner_problem("linux-bundles.yml", "linux-bundles", "ubuntu-22.04") is None
    # Anywhere else it is rejected: another job of the same workflow, another workflow.
    assert runner_problem("linux-bundles.yml", "other", "ubuntu-22.04")
    assert runner_problem("ci.yml", "test", "ubuntu-22.04")
    assert runner_problem("desktop.yml", "desktop-rust", "ubuntu-22.04")
    # And the exception does not open the door to other pinned images or to self-hosted runners.
    assert runner_problem("linux-bundles.yml", "linux-bundles", "ubuntu-20.04")
    assert runner_problem("linux-bundles.yml", "linux-bundles", "ubuntu-24.04")
    assert runner_problem("linux-bundles.yml", "linux-bundles", "self-hosted")
    assert runner_problem("ci.yml", "test", "macos-14")
    assert runner_problem("ci.yml", "test", ["self-hosted", "linux"])
    assert runner_problem("ci.yml", "test", "${{ vars.RUNS_ON }}")
    for label in sorted(HOSTED_LABELS):
        assert runner_problem("ci.yml", "test", label) is None


def test_only_the_linux_bundles_job_runs_on_a_pinned_image():
    pinned = {}
    for path in WORKFLOWS:
        for name, job in (yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}).items():
            if job.get("runs-on") not in HOSTED_LABELS:
                pinned[(path.name, name)] = job.get("runs-on")
    assert pinned == PINNED_IMAGE_EXCEPTIONS


def test_only_the_bundle_workflows_upload_artifacts_and_only_for_three_days():
    uploaders = set()
    for path in WORKFLOWS:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if str(step.get("uses", "")).startswith("actions/upload-artifact@"):
                    uploaders.add(path.name)
                    assert step["with"]["retention-days"] <= 3, f"{path.name}:{name}: keep artifacts 3 days at most"
    assert uploaders == BUNDLE_WORKFLOWS


def test_windows_latest_is_the_runner_of_the_windows_bundles_job_and_no_pinned_image_is_allowed_there():
    assert runner_problem("windows-bundles.yml", "windows-bundles", "windows-latest") is None
    for label in ("windows-2022", "windows-2019", "windows-11-arm", "self-hosted", "ubuntu-22.04"):
        assert runner_problem("windows-bundles.yml", "windows-bundles", label)
