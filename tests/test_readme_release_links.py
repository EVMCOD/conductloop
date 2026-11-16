"""The README must not send people to a download that is six versions old.

The Linux and Windows rows of the download table linked to the assets of one
fixed release (v1.2.23). The Linux and Windows file names carry the version
(`ConductLoop_<version>_amd64.deb`), so the link had to be rewritten by hand at
every release, and nobody did: the README kept offering the binaries of an
old version. The macOS row has no such problem: it links to
`releases/latest/download/ConductLoop-macos-aarch64.dmg`, a file name that does
not change, and it follows the release on its own.

A version-pinned download link is therefore a bug waiting for the next release,
and this test is the guard: no document may carry one. The templates
(`v<version>`, `v<VERSION>`) are not links and are allowed to stay.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs"

# A download link pinned to a release: the tag of the release is a version.
PINNED_RELEASE = re.compile(r"releases/download/v\d+\.\d+\.\d+/")
# Any version inside a link to the releases repo (a tag, an asset name, ...).
VERSION_IN_LINK = re.compile(r"v\d+\.\d+\.\d+")


def _markdown_files() -> list[Path]:
    """The README and every markdown file under docs/."""
    return [README] + sorted(DOCS.rglob("*.md"))


def _links(text: str) -> list[str]:
    """The URLs of the markdown links of a text."""
    return re.findall(r"\]\((https?://[^)\s]+)\)", text)


def test_no_document_links_to_a_pinned_release_asset():
    """A link to `releases/download/v1.2.23/...` is stale by definition."""
    offenders = []
    for path in _markdown_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PINNED_RELEASE.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, (
        "these links point to one fixed release and go stale at the next one: "
        f"{offenders}. Link to the latest release page instead."
    )


def test_a_template_is_not_a_pinned_link():
    """`v<version>` is a placeholder in a doc, not a link to a release."""
    template = "https://github.com/EVMCOD/conductloop-releases/releases/download/v<version>/<asset>"
    assert not PINNED_RELEASE.search(template)
    assert not PINNED_RELEASE.search(
        "https://github.com/EVMCOD/conductloop-releases/releases/download/v<VERSION>/ConductLoop-<VERSION>.dmg"
    )


def test_the_readme_links_to_the_releases_carry_no_version():
    """Every link of the README follows the release on its own."""
    stale = []
    for url in _links(README.read_text(encoding="utf-8")):
        if "conductloop-releases" not in url:
            continue
        if VERSION_IN_LINK.search(url):
            stale.append(url)
    assert not stale, (
        "these README links carry a version and will be out of date at the next "
        f"release: {stale}"
    )


def test_the_linux_and_windows_rows_point_at_the_latest_release():
    """The two rows that could not follow the release now do."""
    text = README.read_text(encoding="utf-8")
    latest = "https://github.com/EVMCOD/conductloop-releases/releases/latest"
    for label in ("Linux", "Windows"):
        row = next((line for line in text.splitlines() if line.startswith(f"| **{label}**")), None)
        assert row, f"the README has no {label} row in the download table"
        assert latest in row, f"the {label} row does not link to the latest release: {row}"
