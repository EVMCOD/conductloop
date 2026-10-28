#!/usr/bin/env python3
"""Dry-run pre-flight check for a desktop release. Publishes nothing.

Run it before ``scripts/release-desktop.sh`` (see docs/RELEASING.md)::

    python3 scripts/release_check.py --version 1.2.1

It is strictly read-only: it reads files in the working tree and runs three
read-only git commands (``status``, ``rev-parse``, ``tag --list``). It does not
publish, does not create tags or releases, does not touch the network and does
not call any API (see tests/test_release_check.py, which also guards this).

Checks (each prints OK, WARN or FAIL; the exit status is 1 if any is FAIL):

1. version      The version is the same everywhere it appears: pyproject.toml,
                ops_dashboard/__init__.py, desktop/src-tauri/tauri.conf.json,
                desktop/src-tauri/Cargo.toml and desktop/package.json (if it
                exists). Cargo.lock and package-lock.json only WARN.
2. changelog    CHANGELOG.md has a ``## ...`` heading for the version that
                also carries an ISO date (``## 1.2.1 - 2026-11-02 - Title``).
3. notes        The release notes are not empty: the body of that CHANGELOG
                section, or the file given with ``--notes-file``.
4. updater-key  The minisign public key in tauri.conf.json has key id
                770EA4C292DC6BD2 (see "How the key id is checked" below).
5. macos        ``bundle.macOS.minimumSystemVersion`` is "13.0".
6. endpoint     The updater endpoints include
                https://github.com/EVMCOD/conductloop-releases/releases/latest/download/update-macos-aarch64.json
7. git          The working tree is clean (no modified, staged or untracked
                files). WARN if the branch is not ``main`` or the tag already
                exists locally.

How the key id is checked
-------------------------
``plugins.updater.pubkey`` in tauri.conf.json is base64 of the text of a
minisign ``.pub`` file::

    untrusted comment: minisign public key: 770EA4C292DC6BD2
    RWTSa9yS... (base64 of 42 bytes)

The second line decodes to 42 bytes: the 2-byte algorithm ``Ed``, an 8-byte key
id stored little-endian, and the 32-byte Ed25519 public key. The key id is those
8 bytes reversed and printed as upper-case hex. That is what minisign (and so
Tauri's updater) compares against the key id of a signature, so it is derived
from the key bytes themselves; the comment line is only a label that anybody
can edit, and it must agree with the bytes or the check fails. The decoding is
the one in scripts/check_updater_pubkey.py, which CI already runs against
desktop/UPDATER_KEY_ID; here the expected id is also fixed in this file
(EXPECTED_KEY_ID) so changing the pin and the key together cannot slip through.
If the key id could not be derived the check would fail, never pass on the
comment alone.

Stdlib only (Python 3.11+).
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_KEY_ID = "770EA4C292DC6BD2"
EXPECTED_MIN_MACOS = "13.0"
EXPECTED_ENDPOINT = (
    "https://github.com/EVMCOD/conductloop-releases/releases/latest/download/"
    "update-macos-aarch64.json"
)
CARGO_PACKAGE = "conductloop-desktop"

OK, WARN, FAIL = "OK", "WARN", "FAIL"

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


@dataclass(frozen=True)
class Result:
    level: str
    check: str
    message: str


def _load_pubkey_module():
    """scripts/check_updater_pubkey.py, loaded from this script's directory."""
    path = Path(__file__).resolve().parent / "check_updater_pubkey.py"
    spec = importlib.util.spec_from_file_location("check_updater_pubkey", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── 1. versions ──────────────────────────────────────────────────────────────


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _version_pyproject(root: Path) -> str:
    return tomllib.loads(_read(root / "pyproject.toml"))["project"]["version"]


def _version_init(root: Path) -> str:
    m = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", _read(root / "ops_dashboard" / "__init__.py"), re.M)
    if not m:
        raise ValueError("no __version__ assignment")
    return m.group(1)


def _version_tauri(root: Path) -> str:
    return json.loads(_read(root / "desktop" / "src-tauri" / "tauri.conf.json"))["version"]


def _version_cargo(root: Path) -> str:
    return tomllib.loads(_read(root / "desktop" / "src-tauri" / "Cargo.toml"))["package"]["version"]


def _version_package_json(root: Path) -> str:
    return json.loads(_read(root / "desktop" / "package.json"))["version"]


def _version_cargo_lock(root: Path) -> str:
    lock = tomllib.loads(_read(root / "desktop" / "src-tauri" / "Cargo.lock"))
    for pkg in lock.get("package", []):
        if pkg.get("name") == CARGO_PACKAGE:
            return pkg["version"]
    raise ValueError(f"no [[package]] named {CARGO_PACKAGE}")


def _version_package_lock(root: Path) -> str:
    data = json.loads(_read(root / "desktop" / "package-lock.json"))
    versions = {data.get("version"), data.get("packages", {}).get("", {}).get("version")} - {None}
    if len(versions) != 1:
        raise ValueError(f"inconsistent top-level versions {sorted(versions)}")
    return versions.pop()


# (label, relative path, reader, required, level when it does not match)
VERSION_SOURCES = [
    ("pyproject.toml", "pyproject.toml", _version_pyproject, True, FAIL),
    ("ops_dashboard/__init__.py", "ops_dashboard/__init__.py", _version_init, True, FAIL),
    ("desktop/src-tauri/tauri.conf.json", "desktop/src-tauri/tauri.conf.json", _version_tauri, True, FAIL),
    ("desktop/src-tauri/Cargo.toml", "desktop/src-tauri/Cargo.toml", _version_cargo, True, FAIL),
    ("desktop/package.json", "desktop/package.json", _version_package_json, False, FAIL),
    ("desktop/src-tauri/Cargo.lock", "desktop/src-tauri/Cargo.lock", _version_cargo_lock, False, WARN),
    ("desktop/package-lock.json", "desktop/package-lock.json", _version_package_lock, False, WARN),
]


def check_versions(root: Path, version: str) -> list[Result]:
    results: list[Result] = []
    for label, rel, reader, required, bad_level in VERSION_SOURCES:
        if not (root / rel).exists():
            if required:
                results.append(Result(FAIL, "version", f"{label} is missing"))
            else:
                results.append(Result(OK, "version", f"{label} not present, skipped"))
            continue
        try:
            found = reader(root)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            results.append(Result(bad_level, "version", f"{label}: cannot read the version ({exc})"))
            continue
        if found == version:
            results.append(Result(OK, "version", f"{label} = {found}"))
        else:
            results.append(Result(bad_level, "version", f"{label} is {found}, expected {version}"))
    return results


# ── 2/3. changelog and release notes ─────────────────────────────────────────


def _version_in_heading(heading: str, version: str) -> bool:
    return re.search(rf"(?<![\w.])v?{re.escape(version)}(?![\w]|\.\d)", heading) is not None


def changelog_section(text: str, version: str) -> tuple[str, str] | None:
    """(heading, body) of the first ``## `` section whose heading names ``version``."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## ") and _version_in_heading(line, version):
            body: list[str] = []
            for nxt in lines[i + 1 :]:
                if nxt.startswith("## "):
                    break
                body.append(nxt)
            return line, "\n".join(body)
    return None


def _valid_date(heading: str) -> dt.date | None:
    for m in _DATE_RE.finditer(heading):
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
    return None


def check_changelog_and_notes(
    root: Path, version: str, notes_file: Path | None, today: dt.date
) -> list[Result]:
    results: list[Result] = []
    path = root / "CHANGELOG.md"
    section: tuple[str, str] | None = None
    if not path.exists():
        results.append(Result(FAIL, "changelog", "CHANGELOG.md is missing"))
    else:
        text = _read(path)
        section = changelog_section(text, version)
        if section is None:
            results.append(Result(
                FAIL, "changelog",
                f"no '## ...' heading for {version} in CHANGELOG.md "
                f"(rename 'Unreleased' to e.g. '## {version} - YYYY-MM-DD')"))
        else:
            heading = section[0]
            date = _valid_date(heading)
            if date is None:
                results.append(Result(FAIL, "changelog", f"heading has no YYYY-MM-DD date: {heading!r}"))
            elif date > today:
                results.append(Result(WARN, "changelog", f"heading date {date} is in the future: {heading!r}"))
            else:
                results.append(Result(OK, "changelog", f"{heading!r}"))
        unreleased = changelog_section_named(text, "Unreleased")
        if unreleased is not None and unreleased.strip():
            results.append(Result(WARN, "changelog", "'## Unreleased' still has entries; move them under the release if they belong to it"))

    if notes_file is not None:
        try:
            notes = notes_file.read_text(encoding="utf-8")
        except OSError as exc:
            results.append(Result(FAIL, "notes", f"cannot read --notes-file {notes_file}: {exc}"))
            return results
        source = f"--notes-file {notes_file}"
    elif section is not None:
        notes = section[1]
        source = f"CHANGELOG.md section for {version}"
    else:
        results.append(Result(FAIL, "notes", "no release notes: no CHANGELOG section and no --notes-file"))
        return results
    if notes.strip():
        results.append(Result(OK, "notes", f"{source} has {len(notes.split())} words"))
    else:
        results.append(Result(FAIL, "notes", f"{source} is empty"))
    return results


def changelog_section_named(text: str, name: str) -> str | None:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^##\s+\[?{re.escape(name)}\]?\s*$", line, re.I):
            body = []
            for nxt in lines[i + 1 :]:
                if nxt.startswith("## "):
                    break
                body.append(nxt)
            return "\n".join(body)
    return None


# ── 4/5/6. tauri.conf.json ───────────────────────────────────────────────────


def _tauri_conf(root: Path) -> dict:
    return json.loads(_read(root / "desktop" / "src-tauri" / "tauri.conf.json"))


def check_updater_key(root: Path) -> list[Result]:
    try:
        cup = _load_pubkey_module()
        conf = root / "desktop" / "src-tauri" / "tauri.conf.json"
        keyid = cup.pubkey_keyid(cup.embedded_pubkey(conf))
    except (ImportError, OSError, ValueError) as exc:  # KeyCheckError is a ValueError
        return [Result(FAIL, "updater-key", f"cannot derive the key id from tauri.conf.json: {exc}")]
    results: list[Result] = []
    if keyid == EXPECTED_KEY_ID:
        results.append(Result(OK, "updater-key", f"pubkey key id {keyid} (derived from the key bytes)"))
    else:
        results.append(Result(
            FAIL, "updater-key",
            f"pubkey key id is {keyid}, expected {EXPECTED_KEY_ID}: installed apps would reject updates"))
    pin = root / "desktop" / "UPDATER_KEY_ID"
    if pin.exists():
        pinned = _read(pin).strip().upper()
        if pinned == EXPECTED_KEY_ID:
            results.append(Result(OK, "updater-key", f"desktop/UPDATER_KEY_ID pins {pinned}"))
        else:
            results.append(Result(FAIL, "updater-key", f"desktop/UPDATER_KEY_ID is {pinned!r}, expected {EXPECTED_KEY_ID}"))
    else:
        results.append(Result(WARN, "updater-key", "desktop/UPDATER_KEY_ID is missing"))
    return results


def check_min_macos(root: Path) -> list[Result]:
    try:
        value = _tauri_conf(root)["bundle"]["macOS"]["minimumSystemVersion"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [Result(FAIL, "macos", f"cannot read bundle.macOS.minimumSystemVersion: {exc}")]
    if value == EXPECTED_MIN_MACOS:
        return [Result(OK, "macos", f"minimumSystemVersion = {value}")]
    return [Result(FAIL, "macos", f"minimumSystemVersion is {value!r}, expected {EXPECTED_MIN_MACOS!r}")]


def check_endpoint(root: Path) -> list[Result]:
    try:
        endpoints = _tauri_conf(root)["plugins"]["updater"]["endpoints"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [Result(FAIL, "endpoint", f"cannot read plugins.updater.endpoints: {exc}")]
    if not isinstance(endpoints, list) or not all(isinstance(e, str) for e in endpoints):
        return [Result(FAIL, "endpoint", "plugins.updater.endpoints is not a list of strings")]
    if EXPECTED_ENDPOINT not in endpoints:
        return [Result(FAIL, "endpoint", f"endpoints {endpoints} do not include {EXPECTED_ENDPOINT}")]
    parts = urlsplit(EXPECTED_ENDPOINT)
    results = [Result(OK, "endpoint", f"{parts.netloc}{parts.path}")]
    extra = [e for e in endpoints if e != EXPECTED_ENDPOINT]
    if extra:
        results.append(Result(WARN, "endpoint", f"unexpected extra endpoints: {extra}"))
    return results


# ── 7. git ───────────────────────────────────────────────────────────────────


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Read-only commands only; tests/test_release_check.py guards the list.
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)


def check_git(root: Path, version: str) -> list[Result]:
    try:
        status = _git(root, "status", "--porcelain", "--untracked-files=normal")
    except OSError as exc:
        return [Result(FAIL, "git", f"cannot run git: {exc}")]
    if status.returncode != 0:
        return [Result(FAIL, "git", f"not a git work tree: {status.stderr.strip() or 'git status failed'}")]
    results: list[Result] = []
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    if dirty:
        shown = "; ".join(dirty[:5]) + (f"; ... (+{len(dirty) - 5} more)" if len(dirty) > 5 else "")
        results.append(Result(FAIL, "git", f"working tree is not clean: {shown}"))
    else:
        results.append(Result(OK, "git", "working tree is clean"))
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main":
        results.append(Result(WARN, "git", f"on branch {branch or '(unknown)'!r}, releases are cut from main"))
    tag = f"v{version}"
    if _git(root, "tag", "--list", tag).stdout.strip():
        results.append(Result(WARN, "git", f"tag {tag} already exists locally"))
    return results


# ── driver ───────────────────────────────────────────────────────────────────


def run_checks(
    root: Path, version: str, notes_file: Path | None = None, today: dt.date | None = None
) -> list[Result]:
    today = today or dt.date.today()
    results: list[Result] = []
    results += check_versions(root, version)
    results += check_changelog_and_notes(root, version, notes_file, today)
    results += check_updater_key(root)
    results += check_min_macos(root)
    results += check_endpoint(root)
    results += check_git(root, version)
    return results


def render(results: list[Result], color: bool) -> str:
    paint = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}
    lines = []
    for r in results:
        tag = f"{r.level:<4}"
        if color:
            tag = f"{paint[r.level]}{tag}\033[0m"
        lines.append(f"{tag}  {r.check:<11} {r.message}")
    return "\n".join(lines)


def exit_code(results: list[Result], strict: bool = False) -> int:
    levels = {r.level for r in results}
    return 1 if FAIL in levels or (strict and WARN in levels) else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--version", required=True, help="version to release, X.Y.Z (no leading v)")
    p.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root (default: this checkout)")
    p.add_argument("--notes-file", type=Path, help="release notes file; default: the CHANGELOG.md section")
    p.add_argument("--strict", action="store_true", help="treat WARN as a failure")
    args = p.parse_args(argv)

    if not _VERSION_RE.match(args.version):
        print(f"FAIL  --version must look like X.Y.Z (got {args.version!r})", file=sys.stderr)
        return 2
    root = args.root.resolve()
    results = run_checks(root, args.version, args.notes_file)
    print(f"Release pre-flight for {args.version} ({root}) - dry run, nothing is published")
    print(render(results, color=sys.stdout.isatty()))
    counts = {lvl: sum(r.level == lvl for r in results) for lvl in (OK, WARN, FAIL)}
    print(f"\n{counts[OK]} OK, {counts[WARN]} WARN, {counts[FAIL]} FAIL")
    code = exit_code(results, args.strict)
    print("Ready to run scripts/release-desktop.sh." if code == 0 else "NOT ready: fix the FAIL lines above.")
    return code


if __name__ == "__main__":
    sys.exit(main())
