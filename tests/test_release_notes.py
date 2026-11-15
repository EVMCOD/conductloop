"""scripts/release_notes.py: the release notes of a release, built from CHANGELOG.md.

The notes used to be written by hand, by copying the previous release. These
tests pin what the builder must produce: the changelog entries, a downloads
table with every platform and its real download URL, and a loud failure when
the changelog has nothing to say about the version.

Every test builds a throwaway changelog in ``tmp_path``; nothing here touches
the real checkout, the network, GitHub or any credential.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_notes.py"
_spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
assert _spec and _spec.loader
rn = importlib.util.module_from_spec(_spec)
sys.modules["release_notes"] = rn  # dataclasses looks the module up here
_spec.loader.exec_module(rn)

VERSION = "1.2.29"

CHANGELOG = f"""# ConductLoop — Changelog

User-facing changes to ConductLoop, newest first.

## {VERSION} — 2026-11-16 — The thing that changed

### The thing that changed

- **The first thing.** It works now.
- The second thing.

### Another thing

- And another one.

## 1.2.28 — 2026-11-15 — The previous release

- Older entry.
"""


@pytest.fixture()
def changelog(tmp_path: Path) -> Path:
    p = tmp_path / "CHANGELOG.md"
    p.write_text(CHANGELOG, encoding="utf-8")
    return p


def build(changelog: Path, version: str = VERSION, repo: str = rn.RELEASE_REPO) -> str:
    return rn.build_notes(version, changelog.read_text(encoding="utf-8"), repo)


# ── what the notes say ───────────────────────────────────────────────────────


def test_the_notes_carry_the_changelog_entries(changelog):
    notes = build(changelog)
    assert "The first thing" in notes and "It works now." in notes
    assert "And another one." in notes


def test_the_subsections_of_the_changelog_become_bold_lines(changelog):
    """The changelog writes its subsections as headings; the release notes are
    a flat list under one heading, so they become bold lines."""
    notes = build(changelog)
    assert "**Another thing**" in notes
    assert "### Another thing" not in notes


def test_the_subsection_that_repeats_the_title_is_dropped(changelog):
    """The changelog opens most releases with the title again; the notes
    already say which release they are, so the title is not repeated."""
    notes = build(changelog)
    assert "**The thing that changed**" not in notes
    assert "The thing that changed" not in notes


def test_the_previous_release_is_mentioned(changelog):
    assert "From 1.2.28: The previous release." in build(changelog)


def test_the_notes_have_the_sections_the_published_releases_have(changelog):
    notes = build(changelog)
    assert notes.startswith(f"ConductLoop {VERSION} for macOS, Linux and Windows.")
    assert f"## What's new in {VERSION}" in notes
    assert "## Downloads" in notes
    assert "## Linux and Windows: what to know" in notes
    assert "**Verify a Linux or Windows download** against `SHA256SUMS`:" in notes
    assert "Full list: [CHANGELOG](https://github.com/EVMCOD/conductloop/blob/main/CHANGELOG.md)." in notes


def test_the_notes_never_leave_an_empty_line_run(changelog):
    body = build(changelog).split("## What's new in")[1].split("Full list:")[0]
    assert "\n\n\n" not in body


# ── the downloads table ──────────────────────────────────────────────────────


def test_the_downloads_table_lists_every_platform(changelog):
    notes = build(changelog)
    table = notes.split("## Downloads")[1].split("You also need a model")[0]
    for name in (
        f"ConductLoop-{VERSION}.dmg",
        f"ConductLoop_{VERSION}_amd64.deb",
        f"ConductLoop_{VERSION}_amd64.AppImage",
        f"ConductLoop_{VERSION}_x64-setup.exe",
        f"ConductLoop_{VERSION}_x64_en-US.msi",
    ):
        assert name in table, name


def test_every_file_of_the_table_links_to_its_download_url(changelog):
    notes = build(changelog)
    for url in rn.download_urls(VERSION):
        assert url in notes, url
        assert f"]({url})" in notes, f"{url} is not a link in the table"


def test_the_download_urls_are_the_ones_of_the_tag(changelog):
    base = f"https://github.com/{rn.RELEASE_REPO}/releases/download/v{VERSION}/"
    urls = rn.download_urls(VERSION)
    assert len(urls) == 5
    assert all(u.startswith(base) for u in urls), urls
    assert urls[0].endswith(f"ConductLoop-{VERSION}.dmg")


def test_the_urls_can_be_listed_on_their_own(changelog):
    """scripts/release-platforms.sh checks exactly these URLs."""
    out = subprocess.run(
        [sys.executable, str(SCRIPT), VERSION, "--urls"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert out == rn.download_urls(VERSION)


# ── failures ─────────────────────────────────────────────────────────────────


def test_a_missing_section_fails(changelog, capsys):
    with pytest.raises(rn.NotesError) as exc:
        build(changelog, version="9.9.9")
    assert "9.9.9" in str(exc.value)


def test_an_invalid_version_fails(changelog):
    with pytest.raises(rn.NotesError):
        build(changelog, version="1.2")
    with pytest.raises(rn.NotesError):
        build(changelog, version="v1.2.29")


def test_an_empty_section_fails(tmp_path):
    p = tmp_path / "CHANGELOG.md"
    p.write_text(f"# Changelog\n\n## {VERSION} — 2026-11-16 — Empty\n\n## 1.2.28 — old\n", encoding="utf-8")
    with pytest.raises(rn.NotesError) as exc:
        build(p)
    assert "empty" in str(exc.value)


def test_the_cli_fails_loudly_on_a_missing_section(changelog):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "9.9.9", "--changelog", str(changelog)],
        capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert "9.9.9" in r.stderr
    assert r.stdout == ""


def test_the_cli_fails_loudly_on_a_bad_version(changelog):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "1.2", "--changelog", str(changelog)],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "X.Y.Z" in r.stderr


def test_a_missing_changelog_fails(tmp_path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), VERSION, "--changelog", str(tmp_path / "nope.md")],
        capture_output=True, text=True,
    )
    assert r.returncode == 1 and "nope.md" in r.stderr


# ── the CLI ──────────────────────────────────────────────────────────────────


def test_the_notes_go_to_stdout_by_default(changelog):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), VERSION, "--changelog", str(changelog)],
        capture_output=True, text=True, check=True,
    )
    assert r.stdout.startswith(f"ConductLoop {VERSION} for macOS, Linux and Windows.")


def test_out_writes_the_file(changelog, tmp_path):
    out = tmp_path / "notes.md"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), VERSION, "--changelog", str(changelog), "--out", str(out)],
        capture_output=True, text=True, check=True,
    )
    assert out.read_text(encoding="utf-8").startswith(f"ConductLoop {VERSION} for macOS")
    assert str(out) in r.stderr


def test_the_dry_run_writes_nothing(changelog, tmp_path):
    out = tmp_path / "notes.md"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), VERSION, "--changelog", str(changelog),
         "--out", str(out), "--dry-run"],
        capture_output=True, text=True, check=True,
    )
    assert not out.exists(), "the dry run wrote the file"
    assert "dry run" in r.stdout.lower()
    # It still shows what it would have written.
    assert f"ConductLoop {VERSION} for macOS" in r.stdout


def test_the_dry_run_needs_no_credential(changelog, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    r = subprocess.run(
        [sys.executable, str(SCRIPT), VERSION, "--changelog", str(changelog), "--dry-run"],
        capture_output=True, text=True, env=env, check=True,
    )
    assert r.returncode == 0


def test_it_publishes_nothing_and_touches_no_network():
    """The notes are text: the script must not sign, upload, call an API or
    reach the network."""
    src = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    for banned in ("urllib.request", "http.client", "requests", "socket", "subprocess", "os.system", "shell=True"):
        assert banned not in code, banned
    for word in ("gh ", "gh release", "git push", "token", "TAURI_SIGNING"):
        assert word not in code, word


# ── the changelog is parsed the way the pre-flight check parses it ───────────


@pytest.mark.parametrize("heading", [
    f"## {VERSION} — 2026-11-16 — Title",
    f"## {VERSION} - 2026-11-16 - Title",
    f"## v{VERSION} (2026-11-16)",
    f"## [{VERSION}] - 2026-11-16",
])
def test_it_finds_the_sections_the_preflight_check_finds(tmp_path, heading):
    """The notes and scripts/release_check.py must agree about where a release
    starts: one of them finding a section the other does not is how a release
    ends up with the wrong notes."""
    p = tmp_path / "CHANGELOG.md"
    p.write_text(f"# Changelog\n\n{heading}\n\n- Something useful.\n", encoding="utf-8")
    assert "Something useful." in build(p)


def test_the_preflight_check_finds_the_same_sections(tmp_path):
    p = tmp_path / "CHANGELOG.md"
    p.write_text(CHANGELOG, encoding="utf-8")
    rc_spec = importlib.util.spec_from_file_location("release_check", ROOT / "scripts" / "release_check.py")
    assert rc_spec and rc_spec.loader
    rc = importlib.util.module_from_spec(rc_spec)
    sys.modules["release_check"] = rc
    rc_spec.loader.exec_module(rc)
    text = p.read_text(encoding="utf-8")
    section = rc.changelog_section(text, VERSION)
    assert section is not None
    assert section[1] in rc.changelog_sections(text)[0][1]
    assert build(p).count("The first thing") == 1
