#!/usr/bin/env python3
"""Build (or check) the Tauri updater manifest for the Linux AppImage.

Output: ``update-linux-x86_64.json``, the Linux counterpart of the macOS
``update-macos-aarch64.json`` that ``scripts/release-desktop.sh`` writes::

    {
      "version": "1.2.3",
      "pub_date": "2026-01-01T00:00:00Z",
      "platforms": {
        "linux-x86_64-appimage": {"signature": "<contents of the .sig>", "url": "<AppImage URL>"}
      }
    }

Only the AppImage has an entry. A ``.deb`` is updated through the package
manager, so it is never listed (docs/PORTABILITY.md, 7.2).

What this script does NOT do: it never signs (it has no key and asks for none),
never uploads, never creates a release or a tag, and never touches the macOS
manifest or ``release-desktop.sh``. It reads a ``.sig`` that the maintainer's
``cargo tauri signer sign`` already produced, refuses it unless its key id is the
one embedded in ``tauri.conf.json`` (770EA4C292DC6BD2), and, with ``--appimage``,
verifies the signature over that exact file with the embedded public key. By
default it prints to stdout; ``--output`` writes a file.

Usage::

    python3 scripts/linux_update_manifest.py --version 1.2.3 \\
        --sig-file ConductLoop-1.2.3-x86_64.AppImage.sig \\
        --appimage ConductLoop-1.2.3-x86_64.AppImage \\
        --output update-linux-x86_64.json

    python3 scripts/linux_update_manifest.py --check update-linux-x86_64.json

``--appimage`` needs the ``cryptography`` package (a core dependency); everything
else is stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
DEFAULT_KEY_ID_FILE = ROOT / "desktop" / "UPDATER_KEY_ID"

REPO = "EVMCOD/conductloop-releases"
PLATFORM_KEY = "linux-x86_64-appimage"
MANIFEST_NAME = "update-linux-x86_64.json"
MACOS_MANIFEST_NAME = "update-macos-aarch64.json"

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*\.AppImage$")
_PUB_DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-\d{2}T([01]\d|2[0-3]):[0-5]\d:[0-5]\dZ$")


class ManifestError(ValueError):
    """Anything that makes the manifest unsafe to publish."""


def _load_pubkey_module():
    spec = importlib.util.spec_from_file_location(
        "check_updater_pubkey", Path(__file__).resolve().parent / "check_updater_pubkey.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ManifestError("scripts/check_updater_pubkey.py is missing")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cup = _load_pubkey_module()


def default_asset_name(version: str) -> str:
    return f"ConductLoop-{version}-x86_64.AppImage"


def release_url(repo: str, version: str, asset: str) -> str:
    """The download URL of ``asset`` in the release ``v<version>`` of ``repo``."""
    return f"https://github.com/{repo}/releases/download/v{version}/{asset}"


def _check_version(version: str) -> str:
    if not _VERSION_RE.fullmatch(version or ""):
        raise ManifestError(f"version {version!r} is not X.Y.Z (optionally with a -suffix)")
    return version


def _check_repo(repo: str) -> str:
    if not _REPO_RE.fullmatch(repo or ""):
        raise ManifestError(f"repo {repo!r} is not owner/name")
    return repo


def _check_asset(asset: str) -> str:
    if not _ASSET_RE.fullmatch(asset or ""):
        raise ManifestError(
            f"asset name {asset!r} must be a plain file name ending in .AppImage "
            "(the .deb is not auto-updated and is never listed)"
        )
    return asset


def _check_pub_date(date: str) -> str:
    """``pub_date`` must be a real UTC timestamp, as the updater parses it as
    RFC 3339 and rejects the whole manifest otherwise (``2026-02-31T...`` is
    well-formed text but not a date)."""
    if not _PUB_DATE_RE.fullmatch(date or ""):
        raise ManifestError(f"pub_date {date!r} is not YYYY-MM-DDTHH:MM:SSZ")
    try:
        dt.datetime.strptime(date, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ManifestError(f"pub_date {date!r} is not a real date and time") from None
    return date


def _check_signature_text(sig_text: str, config: Path, expected_keyid: str | None) -> str:
    """Return the signature as the manifest wants it (one line, no newline);
    raise unless it is a minisign signature made by the key the app embeds."""
    sig = sig_text.strip()
    if not sig or "\n" in sig or "\r" in sig:
        raise ManifestError("the signature must be the single base64 line Tauri writes in the .sig file")
    try:
        cup.check(config, expected=expected_keyid, sig_text=sig)
        pubkey_text = cup.embedded_pubkey(config)
    except cup.KeyCheckError as exc:
        raise ManifestError(str(exc)) from exc
    verify_global_signature(sig, pubkey_text)
    return sig


def build_manifest(
    *,
    version: str,
    signature: str,
    url: str,
    pub_date: str,
    notes: str | None = None,
) -> dict:
    """The manifest as a dict (no validation of the signature: see ``make_manifest``)."""
    entry = {"signature": signature, "url": url}
    manifest: dict = {"version": version}
    if notes:
        manifest["notes"] = notes
    manifest["pub_date"] = pub_date
    manifest["platforms"] = {PLATFORM_KEY: entry}
    return manifest


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def verify_global_signature(sig_text: str, pubkey_text: str) -> None:
    """Verify the minisign *global* signature, which covers the signature bytes
    and the trusted comment. The Tauri updater (minisign-verify) rejects a
    signature whose trusted comment or global signature is missing or altered,
    even when the signature over the file is fine. Raises ManifestError."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - cryptography is a core dependency
        raise ManifestError("checking the signature needs the 'cryptography' package") from exc
    try:
        sig_lines = cup._minisign_text(sig_text).splitlines()
        pub = cup._b64decode(cup._minisign_text(pubkey_text).splitlines()[1])
        sig = cup._b64decode(sig_lines[1])
    except (cup.KeyCheckError, IndexError) as exc:
        raise ManifestError(f"cannot read the key or the signature: {exc}") from exc
    prefix = "trusted comment: "
    if len(sig_lines) < 4 or not sig_lines[2].startswith(prefix):
        raise ManifestError("the signature has no trusted comment and global signature (incomplete .sig)")
    try:
        global_sig = cup._b64decode(sig_lines[3])
    except cup.KeyCheckError as exc:
        raise ManifestError(f"the global signature is not valid base64: {exc}") from exc
    if len(pub) != 42 or len(sig) != 74 or len(global_sig) != 64:
        raise ManifestError("not a minisign public key / signature / global signature")
    message = sig[10:] + sig_lines[2][len(prefix):].encode("utf-8")
    try:
        Ed25519PublicKey.from_public_bytes(pub[10:]).verify(global_sig, message)
    except InvalidSignature:
        raise ManifestError(
            "the global signature does not match the trusted comment: the .sig is corrupted or edited "
            "and the updater would reject it"
        ) from None


def verify_file_signature(appimage: Path, sig_text: str, pubkey_text: str) -> str:
    """Verify the minisign signature of ``appimage`` against ``pubkey_text``
    (Tauri's base64 public key). Returns the algorithm used (``ED`` = prehashed,
    what the Tauri updater requires, or legacy ``Ed``); raises ManifestError."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - cryptography is a core dependency
        raise ManifestError("--appimage needs the 'cryptography' package") from exc
    try:
        pub_lines = cup._minisign_text(pubkey_text).splitlines()
        sig_lines = cup._minisign_text(sig_text).splitlines()
        pub = cup._b64decode(pub_lines[1])
        sig = cup._b64decode(sig_lines[1])
    except (cup.KeyCheckError, IndexError) as exc:
        raise ManifestError(f"cannot read the key or the signature: {exc}") from exc
    if len(pub) != 42 or len(sig) != 74:
        raise ManifestError("not a minisign public key / signature")
    algo = sig[:2]
    if algo not in (b"ED", b"Ed"):
        raise ManifestError("unknown minisign signature algorithm")
    if algo == b"Ed":
        raise ManifestError(
            "legacy (non-prehashed) minisign signature: the Tauri updater only accepts prehashed ones"
        )
    if sig[2:10] != pub[2:10]:
        raise ManifestError("the signature was made by another key than the one the app embeds")
    digest = hashlib.blake2b(digest_size=64)
    with appimage.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    try:
        Ed25519PublicKey.from_public_bytes(pub[10:]).verify(sig[10:], digest.digest())
    except InvalidSignature as exc:
        raise ManifestError(f"the signature does not match {appimage.name}") from exc
    verify_global_signature(sig_text, pubkey_text)
    return algo.decode()


def make_manifest(
    *,
    version: str,
    sig_file: Path,
    repo: str = REPO,
    asset: str | None = None,
    notes: str | None = None,
    pub_date: str | None = None,
    config: Path = DEFAULT_CONFIG,
    expected_keyid: str | None = None,
    appimage: Path | None = None,
) -> dict:
    """Validate everything and return the manifest; raise ManifestError otherwise."""
    version = _check_version(version)
    repo = _check_repo(repo)
    asset = _check_asset(asset or default_asset_name(version))
    if expected_keyid is None and DEFAULT_KEY_ID_FILE.is_file() and config == DEFAULT_CONFIG:
        expected_keyid = DEFAULT_KEY_ID_FILE.read_text(encoding="utf-8").strip()
    try:
        sig_text = sig_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read the signature file: {exc}") from exc
    signature = _check_signature_text(sig_text, config, expected_keyid)
    if appimage is not None:
        if appimage.name != asset:
            raise ManifestError(f"--appimage is {appimage.name!r} but the release asset is {asset!r}")
        try:
            verify_file_signature(appimage, signature, cup.embedded_pubkey(config))
        except (OSError, cup.KeyCheckError) as exc:
            raise ManifestError(str(exc)) from exc
    date = _check_pub_date(pub_date or now_utc())
    return build_manifest(
        version=version, signature=signature, url=release_url(repo, version, asset),
        pub_date=date, notes=notes,
    )


def check_manifest(
    manifest: object, *, config: Path = DEFAULT_CONFIG, expected_keyid: str | None = None
) -> str:
    """Check an existing manifest. Returns its version; raises ManifestError."""
    if not isinstance(manifest, dict):
        raise ManifestError("the manifest is not a JSON object")
    version = _check_version(str(manifest.get("version", "")))
    _check_pub_date(str(manifest.get("pub_date", "")))
    platforms = manifest.get("platforms")
    if not isinstance(platforms, dict) or set(platforms) != {PLATFORM_KEY}:
        raise ManifestError(f"platforms must hold exactly {PLATFORM_KEY!r} (AppImage only)")
    entry = platforms[PLATFORM_KEY]
    if not isinstance(entry, dict) or set(entry) != {"signature", "url"}:
        raise ManifestError("the AppImage entry must have exactly 'signature' and 'url'")
    _check_signature_text(str(entry["signature"]), config, expected_keyid)
    m = re.fullmatch(
        rf"^https://github\.com/({_REPO_RE.pattern[1:-1]})/releases/download/v([^/]+)/([^/]+)$",
        str(entry["url"]),
    )
    if not m:
        raise ManifestError("url must be https://github.com/<owner>/<repo>/releases/download/v<version>/<asset>")
    if m.group(2) != version:
        raise ManifestError(f"url is in release v{m.group(2)} but the manifest version is {version}")
    _check_asset(m.group(3))
    return version


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", type=Path, metavar="MANIFEST", help="validate an existing manifest and exit")
    p.add_argument("--version", help="release version, X.Y.Z")
    p.add_argument("--sig-file", type=Path, help="the .sig that `cargo tauri signer sign` wrote for the AppImage")
    p.add_argument("--appimage", type=Path, help="also verify the signature over this AppImage")
    p.add_argument("--asset-name", help="release asset name (default ConductLoop-<version>-x86_64.AppImage)")
    p.add_argument("--repo", default=REPO, help=f"owner/name of the releases repo (default {REPO})")
    p.add_argument("--notes", help="optional release notes text")
    p.add_argument("--pub-date", help="YYYY-MM-DDTHH:MM:SSZ (default: now, UTC)")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="tauri.conf.json holding the pubkey")
    p.add_argument("--expected-keyid", help="key id the signature must come from (default desktop/UPDATER_KEY_ID)")
    p.add_argument("--output", type=Path, help="write the manifest here instead of stdout")
    args = p.parse_args(argv)

    try:
        expected = args.expected_keyid
        if args.check is not None:
            if expected is None and args.config == DEFAULT_CONFIG and DEFAULT_KEY_ID_FILE.is_file():
                expected = DEFAULT_KEY_ID_FILE.read_text(encoding="utf-8").strip()
            try:
                data = json.loads(args.check.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ManifestError(f"cannot read {args.check}: {exc}") from exc
            version = check_manifest(data, config=args.config, expected_keyid=expected)
            print(f"✓ {args.check}: Linux AppImage manifest for {version} is well formed")
            return 0
        if not args.version or args.sig_file is None:
            p.error("--version and --sig-file are required (or use --check)")
        if args.output is not None and args.output.name == MACOS_MANIFEST_NAME:
            raise ManifestError(f"refusing to write {MACOS_MANIFEST_NAME}: that is the macOS manifest")
        manifest = make_manifest(
            version=args.version, sig_file=args.sig_file, repo=args.repo, asset=args.asset_name,
            notes=args.notes, pub_date=args.pub_date, config=args.config, expected_keyid=expected,
            appimage=args.appimage,
        )
    except ManifestError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.write_text(text, encoding="utf-8")
        print(f"✓ wrote {args.output} (nothing was signed, uploaded or published)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
