"""docs/PORTABILITY.md exists, is linked, keeps its sections and cites real files."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "PORTABILITY.md"

SECTIONS = (
    "## 1. Keychain and secrets",
    "## 2. Execution sandbox",
    "## 3. In-app browser",
    "## 4. Paths, processes, permissions",
    "## 5. PyInstaller sidecar per platform",
    "## 6. Native menu and shortcuts",
    "## 7. Updater",
    "## 8. CI",
    "## 9. Everything else that assumes macOS",
    "## Plan de PRs",
    "## Invariantes de macOS",
)
CITED = re.compile(r"`((?:ops_dashboard|desktop|scripts|tests|bin|docs|\.github)/[\w./@-]+\.\w+)`")


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_the_audit_exists_and_has_every_section():
    text = _text()
    for heading in SECTIONS:
        assert heading in text, f"missing section: {heading}"


def test_readme_links_the_audit():
    assert "docs/PORTABILITY.md" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_untested_things_are_marked():
    assert "untested" in _text()


def test_cited_files_exist():
    """A renamed or deleted file must be noticed here, not by a reader later.

    A line that says "(new)" names a file a future PR will create.
    """
    missing = []
    for line in _text().splitlines():
        if "(new)" in line:
            continue
        for rel in CITED.findall(line):
            if not (ROOT / rel).exists():
                missing.append(rel)
    assert not missing, f"PORTABILITY.md cites files that do not exist: {sorted(set(missing))}"


def test_no_user_specific_paths():
    assert not re.search(r"/Users/\w|/home/\w|C:\\Users\\\w", _text())
