"""Offline guard: this repo has no live tests against the license worker.

The license-worker E2E suite (formerly tests/test_license_worker.py) and the
scripts/test-fleet harness were retired on 2026-10-21 after they created trials
in the production D1. The worker's live tests now live in
EVMCOD/conductloop-license (tests/live), opt-in and preview/local only.
"""
from __future__ import annotations

import configparser
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_WORKER_HOST = re.compile(r"conductloop-license\.[a-z0-9-]+\.workers\.dev|license\.conductloop\.com")


def test_pytest_ini_deselects_live_by_default():
    cfg = configparser.ConfigParser()
    cfg.read(ROOT / "pytest.ini")
    assert '-m "not live"' in cfg["pytest"]["addopts"]


def test_live_worker_suites_are_gone():
    assert not (ROOT / "tests" / "test_license_worker.py").exists()
    assert not (ROOT / "scripts" / "test-fleet").exists()


def test_no_test_or_script_targets_the_license_worker():
    offenders = []
    for base in ("tests", "scripts", "bin"):
        for p in (ROOT / base).rglob("*"):
            if p.is_file() and p.suffix in {".py", ".sh", ".js", ".json", ".toml", ".ini"} and p.name != Path(__file__).name:
                if _WORKER_HOST.search(p.read_text(encoding="utf-8", errors="ignore")):
                    offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, offenders
