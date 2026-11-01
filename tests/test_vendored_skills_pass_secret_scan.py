"""No vendored skill may trip the secret scanner.

The secret-scan job is blocking, and it scans the full history, so a pattern
that looks like a credential fails the pipeline even when it is only a shell
variable. The upstream example authenticated with ``curl -u "$KEY:"``, which
the scanner's ``curl-auth-user`` rule flags.

The rule this pins: a vendored skill shows how to authenticate with a header
(``Authorization: Bearer $VAR``), never with a credential-shaped argument to
``-u``. The example stays runnable and stops being ambiguous.
"""
from __future__ import annotations

import re
from pathlib import Path

PACKS = Path(__file__).resolve().parent.parent / "ops_dashboard" / "skills" / "packs"

# curl ... -u <something>: the scanner's curl-auth-user rule fires on this.
CURL_AUTH_USER = re.compile(r"curl\b[^\n]*\s-u\s+\S")
# The form we accept instead.
BEARER_HEADER = re.compile(r"--header\s+\"Authorization:\s*Bearer\s+\$[A-Z_]+\"")


def _skills() -> list[Path]:
    return sorted(PACKS.rglob("SKILL.md"))


def test_no_skill_passes_a_credential_to_curl_with_dash_u():
    for sk in _skills():
        for i, line in enumerate(sk.read_text(encoding="utf-8").splitlines(), 1):
            if CURL_AUTH_USER.search(line):
                raise AssertionError(
                    f"{sk}:{i} passes a credential to curl with -u; use "
                    f'--header "Authorization: Bearer $VAR" instead: {line.strip()[:90]}')


def test_the_bearer_form_is_what_the_examples_use():
    """Not every skill authenticates, but the ones that do must use the header."""
    for sk in _skills():
        text = sk.read_text(encoding="utf-8")
        if "Authorization: Bearer" in text:
            assert BEARER_HEADER.search(text), sk


def test_the_scanner_finds_nothing_in_the_shipped_catalog():
    """The real check: run the scanner over what we ship."""
    import shutil
    import subprocess

    import pytest

    gitleaks = shutil.which("gitleaks")
    if not gitleaks:
        pytest.skip("gitleaks is not installed")
    proc = subprocess.run(
        [gitleaks, "detect", "--no-git", "--source", str(PACKS),
         "--redact", "--no-banner", "--exit-code", "1"],
        capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, (
        f"the secret scanner flags the shipped catalog:\n{proc.stdout[-2000:]}")
