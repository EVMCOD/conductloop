"""The frozen app has to contain the modules the app imports by name.

PyInstaller runs a spec with SPECPATH (the directory the spec lives in) at
the front of ``sys.path`` — not the repo root. So ``import ops_dashboard``
fails inside the spec, and ``collect_submodules("ops_dashboard")`` returns
an **empty list without raising**.

The build then succeeds, the app ships, and every module that is only
reached by a string import is missing from the binary. That is how the
connectors disappeared: the installed app had zero of them, and said
nothing about it. The page showed an empty catalogue and no error anywhere.

Two things are pinned here:

- the spec puts the repo root on ``sys.path`` before collecting, and
- the build **fails loudly** if the package cannot be found, instead of
  shipping a binary that quietly lost a feature.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "desktop" / "conductloop_server.spec"


def _spec_src() -> str:
    return SPEC.read_text(encoding="utf-8")


def test_the_spec_puts_the_repo_root_on_the_path_before_collecting():
    src = _spec_src()
    insert = src.find("sys.path.insert")
    collect = src.find("collect_all(")
    assert insert != -1, (
        "the spec never puts the repo root on sys.path, so collect_all and "
        "collect_submodules cannot find the package")
    assert insert < collect, (
        "the repo root has to be on sys.path BEFORE collecting, not after")


def test_the_build_fails_loudly_when_the_package_is_missing():
    """A build that cannot find the package must not produce a binary."""
    src = _spec_src()
    assert "assert _pkg_submodules" in src, (
        "the spec collects the package and ignores the result: an empty "
        "collection means a binary with no modules, and the build says OK")
    assert "ops_dashboard.connectors_native." in src, (
        "the spec does not check that the connector modules are included")


def test_pyinstaller_is_in_the_development_requirements():
    """The guard below can only run where PyInstaller is installed.

    It was not in requirements-dev.txt, so the guard passed on a machine that
    had PyInstaller and failed in CI, which does not. A guard that only runs
    on some machines is not a guard.
    """
    req = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").lower()
    assert "pyinstaller" in req, (
        "PyInstaller is not in requirements-dev.txt, so the test that checks "
        "the frozen app really contains every connector cannot run in CI")


def test_collecting_from_the_spec_environment_finds_every_connector():
    """The environment the spec really runs in."""
    try:
        from PyInstaller.utils.hooks import collect_submodules
    except ModuleNotFoundError as exc:  # pragma: no cover - environment problem
        raise ModuleNotFoundError(
            "PyInstaller is not installed, so this guard cannot run. It "
            "belongs in requirements-dev.txt."
        ) from exc

    saved = list(sys.path)
    try:
        sys.path = [p for p in sys.path
                    if os.path.abspath(p or ".") != str(ROOT)]
        sys.path.insert(0, str(ROOT / "desktop"))
        without = collect_submodules("ops_dashboard")
        sys.path.insert(0, str(ROOT))
        with_root = collect_submodules("ops_dashboard")
    finally:
        sys.path = saved

    assert not without, (
        "the spec's environment finds no submodules at all — this is why "
        "the frozen app shipped without the connectors")
    native = [m for m in with_root if m.startswith("ops_dashboard.connectors_native.")]
    token = [m for m in with_root if m.startswith("ops_dashboard.connectors_token.")]
    assert len(native) >= 5, native
    assert len(token) >= 6, token
