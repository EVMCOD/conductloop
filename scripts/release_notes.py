#!/usr/bin/env python3
"""Build the release notes of a ConductLoop release out of CHANGELOG.md.

Why this exists
---------------
Publishing a release used to mean writing the same notes by hand every time:
the release page had a title, a "What's new" section, a downloads table with
one row per platform, the "what to know" notes for Linux and Windows, the
SHA256SUMS instructions and the link to the issues. Copying the previous
release and editing it by hand is how a release ends up advertising a file that
was never uploaded, or a version that does not exist.

This script builds that text from the one thing that is always right, the
``## <version>`` section of CHANGELOG.md, and from the asset names the release
really has. It is the same text the published v1.2.27 release carries.

Usage::

    python3 scripts/release_notes.py 1.2.28                # print the notes
    python3 scripts/release_notes.py 1.2.28 --out notes.md # write them
    python3 scripts/release_notes.py 1.2.28 --urls         # the download URLs
    python3 scripts/release_notes.py 1.2.28 --dry-run      # write nothing

What it does NOT do: it never signs anything, never uploads, never creates or
edits a release or a tag, and never touches the network. It reads CHANGELOG.md
and writes text (to stdout, or to the file given with ``--out``).

The changelog is parsed with the same code the pre-flight check uses
(scripts/release_check.py), so the notes and the check always agree about
where a release starts and ends. The download URLs come from the same
function that scripts/release-platforms.sh verifies, so a URL in the notes is
the URL that was checked.

Stdlib only (Python 3.13+, the version the repo requires).
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHANGELOG = ROOT / "CHANGELOG.md"

RELEASE_REPO = "EVMCOD/conductloop-releases"
SOURCE_REPO = "EVMCOD/conductloop"
CHANGELOG_URL = "https://github.com/EVMCOD/conductloop/blob/main/CHANGELOG.md"
ISSUES_URL = "https://github.com/EVMCOD/conductloop/issues"
DOWNLOAD_PAGE = "https://conductloop.com/download"

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# The ``## <version> — <date> — <title>`` heading of a release. The date and
# the title are optional, and the dash may be a hyphen, an en dash or an em
# dash: the changelog has used all three.
_HEADING_RE = re.compile(
    r"^##\s+\[?(?P<version>\d+\.\d+\.\d+)\]?"
    r"(?:\s*[-—–]\s*(?P<date>\d{4}-\d{2}-\d{2}))?"
    r"(?:\s*[-—–]\s*(?P<title>.+?))?\s*$"
)
# A heading inside a release section (### and deeper).
_SUBHEADING_RE = re.compile(r"^(#{3,})\s+(?P<text>.+?)\s*#*$")

# The asset names, per platform. They are the names the builds produce and
# the names the published releases carry (v1.2.27): the DMG is renamed by
# scripts/release-desktop.sh, the other four are the Tauri bundle names.
_ASSET_NAMES = {
    "dmg": "ConductLoop-{version}.dmg",
    "deb": "ConductLoop_{version}_amd64.deb",
    "appimage": "ConductLoop_{version}_amd64.AppImage",
    "setup": "ConductLoop_{version}_x64-setup.exe",
    "msi": "ConductLoop_{version}_x64_en-US.msi",
}
# The order the assets are listed in: the downloads table and the URL check
# both use it, so the table and the check cannot drift apart.
ASSET_ORDER = ("dmg", "deb", "appimage", "setup", "msi")


class NotesError(ValueError):
    """Anything that makes the notes impossible to build."""


def _load_release_check():
    """scripts/release_check.py, loaded from this script's directory.

    It is the owner of the changelog parsing: the pre-flight check and these
    notes must agree about what a release section is, so the parser is not
    written twice.
    """
    path = Path(__file__).resolve().parent / "release_check.py"
    spec = importlib.util.spec_from_file_location("release_check", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise NotesError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_check"] = module  # dataclasses looks the module up here
    spec.loader.exec_module(module)
    return module


def asset_names(version: str) -> dict[str, str]:
    """The release asset name of every platform, for this version."""
    return {key: name.format(version=version) for key, name in _ASSET_NAMES.items()}


def download_url(repo: str, version: str, asset: str) -> str:
    """The download URL of ``asset`` in the release ``v<version>`` of ``repo``."""
    return f"https://github.com/{repo}/releases/download/v{version}/{asset}"


def download_urls(version: str, repo: str = RELEASE_REPO) -> list[str]:
    """Every download URL of the release, in the order the assets are listed."""
    names = asset_names(version)
    return [download_url(repo, version, names[key]) for key in ASSET_ORDER]


def _heading_parts(heading: str) -> tuple[str | None, str | None]:
    """(version, title) of a ``## `` heading, when it carries them."""
    m = _HEADING_RE.match(heading.strip())
    if not m:
        return None, None
    return m.group("version"), m.group("title")


def _notes_body(body: str, release_title: str | None) -> str:
    """The changelog section as the "What's new" part of the notes.

    The changelog writes its subsections as ``###`` headings; the release
    notes are a flat list under one heading, so they become bold lines. A
    subsection that repeats the title of the release is dropped: the changelog
    opens most releases with it, and the notes already say which release they
    are.
    """
    out: list[str] = []
    for line in body.splitlines():
        m = _SUBHEADING_RE.match(line)
        if m:
            text = m.group("text").strip()
            if text and (release_title is None or text.casefold() != release_title.casefold()):
                out.append(f"**{text}**")
            continue
        out.append(line)
    text = "\n".join(out).strip("\n")
    return re.sub(r"\n{3,}", "\n\n", text)


def _downloads_table(version: str, repo: str) -> str:
    """The downloads table: one row per platform, every file a link to it."""
    names = asset_names(version)

    def link(key: str) -> str:
        return f"[`{names[key]}`]({download_url(repo, version, names[key])})"

    rows = [
        "| System | File | Install |",
        "|---|---|---|",
        "| macOS 13+ (Apple Silicon) | "
        f"{link('dmg')} | Open the DMG and drag ConductLoop to Applications. "
        "Signed and notarized; it updates itself. The in-app browser needs macOS 14 (Sonoma). |",
        "| Linux x86_64 (Ubuntu 22.04+) | "
        f"{link('deb')} | `sudo apt install ./{names['deb']}` |",
        "| | "
        f"{link('appimage')} | `chmod +x {names['appimage']} && ./{names['appimage']}` |",
        "| Windows 10/11 x64 | "
        f"{link('setup')} | Run it (per user, no administrator prompt). |",
        "| | "
        f"{link('msi')} | The `.msi` is an alternative, for managed deployments. |",
    ]
    return "\n".join(rows)


def _previous_release_line(sections: list[tuple[str, str]], index: int) -> str | None:
    """The "From <previous version>: <title>" line, when the changelog has one."""
    if index + 1 >= len(sections):
        return None
    version, title = _heading_parts(sections[index + 1][0])
    if not version or not title:
        return None
    return f"From {version}: {title}."


def build_notes(version: str, changelog_text: str, repo: str = RELEASE_REPO) -> str:
    """The release notes of ``version``, as the published releases carry them.

    Raises NotesError when the changelog has no section for the version, or
    when that section is empty: a release with no notes is a release nobody
    can read.
    """
    if not _VERSION_RE.match(version):
        raise NotesError(f"version {version!r} is not X.Y.Z")
    rc = _load_release_check()
    sections = rc.changelog_sections(changelog_text)
    section = rc.changelog_section(changelog_text, version)
    if section is None:
        raise NotesError(
            f"CHANGELOG.md has no '## {version}' section "
            f"(rename 'Unreleased' to e.g. '## {version} - YYYY-MM-DD - Title')"
        )
    heading, body = section
    index = next(i for i, (h, _) in enumerate(sections) if h == heading)
    if not body.strip():
        raise NotesError(f"the '## {version}' section of CHANGELOG.md is empty")
    _, title = _heading_parts(heading)
    whats_new = _notes_body(body, title)
    if not whats_new.strip():
        raise NotesError(f"the '## {version}' section of CHANGELOG.md has nothing but a heading")

    parts: list[str] = [
        f"ConductLoop {version} for macOS, Linux and Windows. Free and open source (MIT): "
        "build your AI team, direct the CEO agent, and connect the apps it can use. "
        "The app runs on your computer; the AI model is the one you choose, with your own key.",
        "",
        f"## What's new in {version}",
        "",
        whats_new,
    ]
    previous = _previous_release_line(sections, index)
    if previous:
        parts += ["", previous]
    parts += [
        "",
        f"Full list: [CHANGELOG]({CHANGELOG_URL}).",
        "",
        "## Downloads",
        "",
        _downloads_table(version, repo),
        "",
        "You also need a model: your own key for Anthropic, OpenAI, MiniMax, OpenRouter or "
        "Vercel AI Gateway, any OpenAI-compatible server, or a local model with Ollama.",
        "",
        "## Linux and Windows: what to know",
        "",
        f"- **They do not update themselves yet.** Download each new version from {DOWNLOAD_PAGE} "
        "or this page.",
        '- **Windows:** the installer is not code-signed, so SmartScreen says "Windows protected '
        'your PC": click **More info**, then **Run anyway**. Connected apps (they need a system '
        "keychain), code sessions and the in-app browser are not available on Windows yet; AI "
        "keys are kept in ConductLoop's encrypted file.",
        "- **Linux:** on Ubuntu 24.04 use the `.deb` (the AppImage can open a blank window there). "
        "Connected apps need a system keyring (GNOME Keyring, KWallet or KeePassXC). Running "
        "commands in code sessions needs bubblewrap (`bwrap`). No in-app browser. Requires "
        "WebKitGTK 4.1 and GTK 3.",
        "- Checked on every build by GitHub's runners: install, start, first window, shutdown (and "
        "on Windows, uninstall keeping your data), on Ubuntu 22.04 and 24.04 and on Windows.",
        "",
        "**Verify a Linux or Windows download** against `SHA256SUMS`:",
        "",
        "```",
        "sha256sum -c SHA256SUMS --ignore-missing                        # Linux",
        f"Get-FileHash -Algorithm SHA256 .\\{asset_names(version)['setup']}  # Windows",
        "```",
        "",
        f"**Problems?** Open an issue at {ISSUES_URL} and say which system you use.",
    ]
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("version", help="release version, X.Y.Z (no leading v)")
    p.add_argument("--changelog", type=Path, default=DEFAULT_CHANGELOG, help="changelog to read")
    p.add_argument("--out", type=Path, help="write the notes to this file instead of stdout")
    p.add_argument("--repo", default=RELEASE_REPO, help=f"owner/name of the releases repo (default {RELEASE_REPO})")
    p.add_argument("--urls", action="store_true", help="print the download URLs (one per line) and exit")
    p.add_argument("--dry-run", action="store_true", help="print what would be written; write nothing")
    args = p.parse_args(argv)

    if not _VERSION_RE.match(args.version):
        print(f"✗ version {args.version!r} is not X.Y.Z", file=sys.stderr)
        return 2

    if args.urls:
        for url in download_urls(args.version, args.repo):
            print(url)
        return 0

    try:
        text = args.changelog.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"✗ cannot read {args.changelog}: {exc}", file=sys.stderr)
        return 1
    try:
        notes = build_notes(args.version, text, args.repo)
    except NotesError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("=== dry run: nothing was written ===")
        if args.out is not None:
            print(f"would write the release notes to: {args.out}")
        else:
            print("would print the release notes to stdout")
        print("--- notes begin ---")
        sys.stdout.write(notes)
        print("--- notes end ---")
        return 0

    if args.out is None:
        sys.stdout.write(notes)
    else:
        args.out.write_text(notes, encoding="utf-8")
        print(f"✓ wrote {args.out} (nothing was signed, uploaded or published)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
