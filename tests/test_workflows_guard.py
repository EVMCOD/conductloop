"""Guards on the CI workflows of this PUBLIC repository.

* Nothing may run on a self-hosted runner. Anyone can open a pull request
  against a public repo, and a self-hosted runner would execute their code on
  a private machine. Runner labels must be literal GitHub-hosted ones: no
  ``self-hosted``, no ``ev-server``, no ``vars.RUNS_ON`` (a repository variable
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
HOSTED = re.compile(r"^(ubuntu|macos|windows)-[a-z0-9.\-]+$")
FORBIDDEN = ("self-hosted", "ev-server", "vars.runs_on", "runs_on")
USES = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
PINNED = re.compile(r"^[\w.\-]+/[\w.\-]+(/[\w.\-/]+)?@[0-9a-f]{40}$")


def _code_lines(path: Path):
    """Lines of the workflow without full-line and trailing comments."""
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = re.sub(r"(^|\s)#.*$", "", line)
        if code.strip():
            yield n, code


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
        runs_on = job.get("runs-on")
        assert isinstance(runs_on, str), f"{path.name}:{name}: runs-on must be a plain label"
        assert HOSTED.match(runs_on), f"{path.name}:{name}: {runs_on!r} is not a GitHub-hosted label"
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
