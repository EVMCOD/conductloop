"""scripts/verify.sh must run what CI runs.

``scripts/verify.sh`` is the local mirror of ``.github/workflows/ci.yml`` (job
``test``) plus the gitleaks step of ``secret-scan.yml``. These tests fail when
the two diverge: a CI step missing from the script (or the other way round), a
changed command, a different order, or a typed-module ratchet list that is no
longer shared. When one fails, update the side that is out of date.
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "scripts" / "verify.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"
SECRET_SCAN = ROOT / ".github" / "workflows" / "secret-scan.yml"
RATCHET = ROOT / "scripts" / "mypy-ratchet.txt"

# CI steps verify.sh deliberately does not run, with the reason. A new CI step
# that is in neither verify.sh nor here fails the test, so adding one forces a
# decision. Steps with a ``uses:`` and no name are listed by their action.
CI_ONLY = {
    "actions/checkout": "checks out the repository; you already have it",
    "actions/setup-python": "installs Python on the runner",
    "Install dev dependencies": "your virtualenv already has requirements-dev.txt",
    "Browser test (headless Chrome) — \"Conectores\" page": (
        "installs and removes Playwright and needs Chrome; CI only"),
    "Install bubblewrap and ffmpeg (real sandbox and real mixing)": (
        "apt install on the runner"),
    "Browser tests (headless Chrome) — every tests/test_*ui_browser.py": (
        "installs and removes Playwright and needs Chrome; CI only"),
}

# Steps whose verify.sh command is intentionally not the CI command, because the
# CI one would modify the developer's environment. {CI step name: verify command}.
EQUIVALENT = {
    "Package builds (pip install .)": 'pip wheel . --no-deps --wheel-dir "$VERIFY_TMP/wheel"',
}


def _verify_steps() -> list[tuple[str, str, str]]:
    """(kind, CI step name, command) rows of the STEPS table in verify.sh."""
    text = VERIFY.read_text(encoding="utf-8")
    block = re.search(r"^STEPS=\(\n(.*?)^\)", text, re.S | re.M)
    assert block, "STEPS=( ... ) table not found in scripts/verify.sh"
    rows = []
    for line in block.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"'(blocking|advisory)\|([^|]+)\|(.+)'", line)
        assert m, f"cannot parse STEPS row: {line!r}"
        rows.append((m.group(1), m.group(2), m.group(3)))
    assert rows
    return rows


def _ci_steps() -> list[dict]:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["test"]["steps"]


def _ci_key(step: dict) -> str:
    return step.get("name") or step["uses"].split("@")[0]


def _tokens(cmd: str) -> list[str]:
    return shlex.split(" ".join(cmd.split()))


def test_every_ci_step_is_in_verify_or_explicitly_ci_only():
    verify_names = {name for _, name, _ in _verify_steps()}
    for step in _ci_steps():
        key = _ci_key(step)
        assert key in verify_names or key in CI_ONLY, (
            f"CI step {key!r} is not in scripts/verify.sh STEPS and not in CI_ONLY "
            "(tests/test_verify_script.py): add it to one of them")


def test_every_verify_step_exists_in_ci():
    ci_names = {_ci_key(s) for s in _ci_steps()}
    for _, name, _ in _verify_steps():
        assert name in ci_names, f"verify.sh step {name!r} does not exist in ci.yml any more"


def test_ci_only_entries_still_exist_in_ci():
    ci_names = {_ci_key(s) for s in _ci_steps()}
    stale = set(CI_ONLY) - ci_names
    assert not stale, f"CI_ONLY lists steps that are no longer in ci.yml: {sorted(stale)}"


def test_commands_match_ci():
    ci = {_ci_key(s): s for s in _ci_steps()}
    for _, name, cmd in _verify_steps():
        step = ci[name]
        expected = EQUIVALENT.get(name, step["run"])
        assert _tokens(cmd) == _tokens(expected), (
            f"{name!r}: verify.sh runs\n  {cmd}\nbut CI runs\n  {expected}")


def test_equivalent_overrides_are_for_real_steps():
    ci_runs = {_ci_key(s): s.get("run", "") for s in _ci_steps()}
    for name in EQUIVALENT:
        assert name in ci_runs, f"EQUIVALENT lists {name!r}, which is not a CI step"


def test_blocking_vs_advisory_matches_continue_on_error():
    ci = {_ci_key(s): s for s in _ci_steps()}
    for kind, name, _ in _verify_steps():
        advisory = bool(ci[name].get("continue-on-error"))
        assert (kind == "advisory") == advisory, (
            f"{name!r}: verify.sh says {kind}, ci.yml continue-on-error={advisory}")


def test_steps_run_in_the_same_order_as_ci():
    ci_order = [_ci_key(s) for s in _ci_steps()]
    verify_order = [name for _, name, _ in _verify_steps()]
    assert verify_order == [n for n in ci_order if n in verify_order]


def test_gitleaks_command_matches_secret_scan():
    doc = yaml.safe_load(SECRET_SCAN.read_text(encoding="utf-8"))
    steps = doc["jobs"]["gitleaks"]["steps"]
    scan = next(s for s in steps if s.get("name", "").startswith("Scan the full history"))
    ci_cmd = scan["run"].replace("./gitleaks", "gitleaks")
    m = re.search(r"^GITLEAKS_CMD='(.+)'$", VERIFY.read_text(encoding="utf-8"), re.M)
    assert m, "GITLEAKS_CMD not found in scripts/verify.sh"
    assert _tokens(m.group(1)) == _tokens(ci_cmd)


def test_mypy_ratchet_list_is_shared_and_valid():
    ci = {_ci_key(s): s for s in _ci_steps()}
    step = ci["Type check (mypy) — blocking (typed-module ratchet)"]
    assert "scripts/mypy-ratchet.txt" in step["run"], "ci.yml must read the shared ratchet list"
    entries = RATCHET.read_text(encoding="utf-8").split()
    assert entries and len(entries) == len(set(entries)), "empty or duplicated ratchet entries"
    for entry in entries:
        assert (ROOT / entry).exists(), f"ratchet lists {entry}, which does not exist"
    assert not any(e.startswith("#") for e in entries), "no comments in the list: $(cat ...) feeds it to mypy"


def test_verify_script_is_executable_shell_that_parses():
    assert VERIFY.stat().st_mode & 0o111, "scripts/verify.sh must be executable"
    for script in (VERIFY, ROOT / "scripts" / "install-hooks.sh", ROOT / ".githooks" / "pre-push"):
        subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_list_option_prints_every_step_without_running_anything():
    out = subprocess.run([str(VERIFY), "--list"], capture_output=True, text=True, check=True).stdout
    for _, name, _ in _verify_steps():
        assert name in out
    assert "gitleaks" in out


def test_offline_package_build_does_not_download_setuptools():
    # pip's isolated build env fetches setuptools from the index, so offline the
    # build must use the installed one (or be skipped when there is none).
    src = VERIFY.read_text()
    block = src[src.index("if [ \"$OFFLINE\" = 1 ] && [ \"$name\" = 'Package builds (pip install .)' ]"):]
    block = block[:block.index("\n  fi\n") + 6]
    assert 'cmd="$cmd --no-build-isolation"' in block
    assert "import setuptools" in block and "record SKIP" in block


def test_unknown_option_is_rejected():
    r = subprocess.run([str(VERIFY), "--nope"], capture_output=True, text=True)
    assert r.returncode == 2 and "unknown option" in r.stderr


def test_makefile_has_verify_targets():
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^verify:", mk, re.M) and "scripts/verify.sh" in mk
