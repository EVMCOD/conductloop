"""The scheduled jobs must run this checkout, not whatever sits in a folder.

Incident (2026-11-15): the seven launchd jobs did ``cd $HOME/ops-dashboard``
and ``python -m ops_dashboard.cli``, so Python imported the ``ops_dashboard/``
folder that happened to be there — a stale copy of the tree, twelve releases
behind — and the daily Google Analytics / Search Console poll died on
``No module named 'google.analytics'`` every single day without anybody
noticing. The fix was in the repository the whole time; the job never ran it.

These tests fail if a job script goes back to depending on the current
directory, or if ``bin/ops_env.sh`` stops putting this checkout first on the
path, or if the sanity check stops catching a wrong checkout.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OPS_ENV = ROOT / "bin" / "ops_env.sh"

# The job scripts that run the app on a schedule. Each one is launched by a
# launchd agent on macOS (or by cron elsewhere).
JOB_SCRIPTS = [
    ROOT / "bin" / "run_recap_cron.sh",
    ROOT / "bin" / "run_digest.sh",
    ROOT / "bin" / "poll_ga.sh",
    ROOT / "bin" / "rotate-logs.sh",
    ROOT / "bin" / "poll_revenue.sh",
    ROOT / "bin" / "check_urls.sh",
    ROOT / "scripts" / "poll_analytics.sh",
]


def _run_bash(script: str, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    full_env = dict(os.environ)
    full_env.update(env)
    # A clean PYTHONPATH: the point of the test is what each script does with
    # it, not what the caller's shell happened to export.
    full_env["PYTHONPATH"] = env.get("PYTHONPATH", "")
    # conftest.py points OPS_DATA_DIR at a sandbox for the whole test session;
    # these tests are about what the scripts do with the variable themselves.
    if "OPS_DATA_DIR" not in env:
        full_env["OPS_DATA_DIR"] = ""
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(cwd) if cwd else None,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _stale_checkout(tmp_path: Path) -> Path:
    """A folder that looks like an old checkout: a package that is not this one."""
    stale = tmp_path / "stale"
    pkg = stale / "ops_dashboard"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.0.1-stale"\n', encoding="utf-8")
    return stale


def test_the_shared_env_exists() -> None:
    assert OPS_ENV.is_file(), "bin/ops_env.sh is missing: the job scripts have nothing to source"


@pytest.mark.parametrize("script", JOB_SCRIPTS, ids=lambda p: p.name)
def test_job_script_sources_the_shared_env(script: Path) -> None:
    assert script.is_file(), f"{script} is missing"
    body = script.read_text(encoding="utf-8")
    assert "ops_env.sh" in body, f"{script.name} does not source bin/ops_env.sh"


@pytest.mark.parametrize("script", JOB_SCRIPTS, ids=lambda p: p.name)
def test_job_script_does_not_cd_into_another_checkout(script: Path) -> None:
    body = script.read_text(encoding="utf-8")
    for pattern in ('cd "$HOME/ops-dashboard"', 'cd "$OPS_HOME"', 'OPS_HOME="$HOME/ops-dashboard"'):
        assert pattern not in body, (
            f"{script.name} still hardcodes the checkout it runs from ({pattern!r}); "
            "the job must run the code of the folder it lives in"
        )


@pytest.mark.parametrize("script", JOB_SCRIPTS, ids=lambda p: p.name)
def test_job_script_picks_the_interpreter_through_the_env(script: Path) -> None:
    body = script.read_text(encoding="utf-8")
    assert "source .venv/bin/activate" not in body, (
        f"{script.name} activates a virtualenv by hand; use $OPS_PYTHON"
    )
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "python" in stripped and "ops_dashboard" in stripped:
            assert "$OPS_PYTHON" in stripped, (
                f"{script.name} runs python without $OPS_PYTHON: {stripped!r}"
            )


@pytest.mark.parametrize("script", JOB_SCRIPTS, ids=lambda p: p.name)
def test_job_script_writes_no_relative_log_path(script: Path) -> None:
    body = script.read_text(encoding="utf-8")
    assert '>> "logs/' not in body, f"{script.name} writes to a relative log path"
    assert '>> "$LOG_DIR/' not in body, f"{script.name} writes to a log path of its own"


def test_env_imports_this_checkout_from_anywhere(tmp_path: Path) -> None:
    """From any working directory, the import must land on this checkout."""
    stale = _stale_checkout(tmp_path)
    result = _run_bash(
        f'. "{OPS_ENV}"; "$OPS_PYTHON" -c '
        "'import ops_dashboard; print(ops_dashboard.__file__)'",
        env={"OPS_PYTHON": sys.executable},
        cwd=stale,
    )
    assert result.returncode == 0, result.stderr
    loaded = Path(result.stdout.strip().splitlines()[-1]).resolve()
    assert loaded.parent.parent == ROOT.resolve(), (
        f"the job would import {loaded}, not the checkout at {ROOT}"
    )


def test_env_moves_the_job_to_this_checkout(tmp_path: Path) -> None:
    """Sourcing the helper leaves the job standing in the checkout.

    Python puts the current directory first on sys.path, ahead of PYTHONPATH,
    so this is the part that actually closes the trap.
    """
    stale = _stale_checkout(tmp_path)
    result = _run_bash(
        f'. "{OPS_ENV}"; pwd',
        env={"OPS_PYTHON": sys.executable},
        cwd=stale,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip().splitlines()[-1]).resolve() == ROOT.resolve()

def test_without_the_env_the_current_directory_wins(tmp_path: Path) -> None:
    """The control: without the helper, Python imports whatever is underfoot.

    This is the trap the helper closes. If this environment resolves the
    package from site-packages even with a foreign working directory, the trap
    cannot be reproduced here and there is nothing to compare against.
    """
    stale = _stale_checkout(tmp_path)
    result = _run_bash(
        f'cd "{stale}"; "{sys.executable}" -c '
        "'import ops_dashboard; print(ops_dashboard.__file__)'",
        env={"OPS_PYTHON": sys.executable},
        cwd=stale,
    )
    if result.returncode != 0:
        pytest.skip(f"ops_dashboard is not importable in this environment: {result.stderr.strip()}")
    loaded = Path(result.stdout.strip().splitlines()[-1]).resolve()
    if loaded.parent.parent == ROOT.resolve():
        pytest.skip("this environment resolves ops_dashboard from site-packages; the trap is not reproducible")
    assert loaded.parent.parent == stale.resolve(), (
        f"expected the stale copy under {stale}, got {loaded}"
    )


def test_the_sanity_check_passes_here() -> None:
    result = _run_bash(
        f'. "{OPS_ENV}"; ops_check_code',
        env={"OPS_PYTHON": sys.executable},
    )
    assert result.returncode == 0, result.stderr
    assert "code ok" in result.stdout


def test_the_sanity_check_catches_a_wrong_checkout(tmp_path: Path) -> None:
    """A checkout that is not the one the job expects must fail loudly."""
    fake = tmp_path / "elsewhere"
    (fake / "bin").mkdir(parents=True)
    (fake / "bin" / "ops_env.sh").write_text(OPS_ENV.read_text(encoding="utf-8"), encoding="utf-8")
    result = _run_bash(
        f'. "{fake / "bin" / "ops_env.sh"}"; ops_check_code',
        env={"OPS_PYTHON": sys.executable},
    )
    assert result.returncode != 0, "a job running the wrong code must not pass the check"
    assert "WRONG CODE" in result.stderr, result.stderr


def test_the_data_directory_is_named_explicitly(tmp_path: Path) -> None:
    """The job must say which database it writes, not let app_paths guess."""
    live = tmp_path / "ops-dashboard" / "data"
    live.mkdir(parents=True)
    (live / "ops.db").write_bytes(b"")
    result = _run_bash(
        f'. "{OPS_ENV}"; echo "$OPS_DATA_DIR"',
        env={"OPS_PYTHON": sys.executable, "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip().splitlines()[-1]).resolve() == live.resolve(), (
        "the job would not write to the live database"
    )


def test_an_explicit_data_directory_wins(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    result = _run_bash(
        f'. "{OPS_ENV}"; echo "$OPS_DATA_DIR"',
        env={"OPS_PYTHON": sys.executable, "OPS_DATA_DIR": str(other)},
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip().splitlines()[-1]).resolve() == other.resolve()
