"""Guards on the Windows CI job (.github/workflows/windows.yml).

It is a compile-and-test job and must stay one: no signing, no release, no
artifact, no secret, not a required check. And the list of tests skipped on
Windows must stay honest: every entry exists and says why.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
WORKFLOW = ROOT / ".github" / "workflows" / "windows.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
DOC = yaml.safe_load(TEXT)
sys.path.insert(0, str(TESTS))

from windows_skips import SKIPPED_ON_WINDOWS  # noqa: E402


def _steps():
    for name, job in DOC["jobs"].items():
        for step in job["steps"]:
            yield name, step


def test_every_job_runs_on_windows_latest_with_a_timeout():
    assert set(DOC["jobs"]) == {"windows-python", "windows-rust"}
    for name, job in DOC["jobs"].items():
        assert job["runs-on"] == "windows-latest", name
        assert job["timeout-minutes"] <= 60, name


def test_it_cannot_publish_sign_or_read_secrets():
    code = "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in TEXT.splitlines())
    for word in ("secrets.", "upload-artifact", "gh release", "tauri build", "tauri-action",
                 "signtool", "codesign", "notariz", "APPLE_", "TAURI_SIGNING", "GITHUB_TOKEN",
                 "release-desktop", "git push", "vars."):
        assert word not in code, f"{word!r} does not belong in a compile-and-test job"
    assert DOC["permissions"] == {"contents": "read"}
    for _, step in _steps():
        assert "env" not in step or not any("secret" in str(v).lower() for v in step["env"].values())


def test_it_is_a_check_only_job_for_rust():
    runs = [s.get("run", "") for n, s in _steps() if n == "windows-rust"]
    joined = "\n".join(runs)
    assert "cargo check --locked --all-targets" in joined
    assert "cargo build" not in joined and "tauri" not in joined.replace("src-tauri", "").replace("tauri-build", "")


def test_the_python_job_runs_pytest_without_the_live_worker():
    runs = "\n".join(s.get("run", "") for n, s in _steps() if n == "windows-python")
    assert 'pytest -m "not live"' in runs
    assert "--ignore=tests/test_license_worker.py" in runs


def test_it_is_not_a_required_check():
    """Required checks live in branch protection, not in the repo; the names a
    job reports must not collide with the two that are required there."""
    for job in DOC["jobs"].values():
        assert job.get("name", "") not in {"test", "gitleaks"}
    assert set(DOC["jobs"]).isdisjoint({"test", "gitleaks"})


def test_every_skipped_file_exists_and_has_a_reason():
    assert SKIPPED_ON_WINDOWS
    for name, reason in SKIPPED_ON_WINDOWS.items():
        file, _, test = name.partition("::")
        assert (TESTS / file).is_file(), f"{name} is listed but does not exist"
        if test:
            assert re.search(rf"^\s*def {re.escape(test)}\(", (TESTS / file).read_text(encoding="utf-8"), re.M), \
                f"{name}: no such test"
        assert len(reason.split()) >= 4, f"{name}: say why it cannot run on Windows"
    assert not any(n.startswith("test_windows") for n in SKIPPED_ON_WINDOWS), \
        "the Windows tests themselves must run on Windows"


def _items(*names):
    return [SimpleNamespace(fspath=str(TESTS / n), name="test_x", marks=[], add_marker=lambda m, _n=n: marks.setdefault(_n, []).append(m))
            for n in names]


marks: dict[str, list] = {}


def test_the_conftest_hook_skips_only_on_windows(monkeypatch):
    import conftest
    victim = next(iter(SKIPPED_ON_WINDOWS))
    items = _items(victim, "test_auth.py")
    node = next(n for n in SKIPPED_ON_WINDOWS if "::" in n)
    nfile, _, ntest = node.partition("::")
    items.append(SimpleNamespace(fspath=str(TESTS / nfile), name=ntest, marks=[],
                                 add_marker=lambda m: marks.setdefault(node, []).append(m)))
    items.append(SimpleNamespace(fspath=str(TESTS / nfile), name="test_some_other_test", marks=[],
                                 add_marker=lambda m: marks.setdefault("other", []).append(m)))

    marks.clear()
    monkeypatch.setattr(sys, "platform", "linux")
    conftest.pytest_collection_modifyitems(None, items)
    assert marks == {}, "nothing may be skipped on macOS/Linux"

    monkeypatch.setattr(sys, "platform", "win32")
    conftest.pytest_collection_modifyitems(None, items)
    assert sorted(marks) == sorted([victim, node])
    mark = marks[victim][0]
    assert mark.name == "skip" and SKIPPED_ON_WINDOWS[victim] in mark.kwargs["reason"]

