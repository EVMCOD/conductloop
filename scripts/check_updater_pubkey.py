#!/usr/bin/env python3
"""Fail fast when the Tauri updater pubkey does not match the signing key.

The desktop app verifies every update against the minisign public key embedded
in ``desktop/src-tauri/tauri.conf.json`` (``plugins.updater.pubkey``). If a
release is signed with any other key, installed apps silently refuse to update
(this is exactly what happened to v1.1.1 / v1.2.0). This script compares minisign
*key ids* — it only ever reads PUBLIC material (pubkey / .pub / .sig files),
never a private key.

Usage::

    # CI: embedded pubkey vs the key id pinned in the repo
    python3 scripts/check_updater_pubkey.py --expected-file desktop/UPDATER_KEY_ID

    # release-desktop.sh: embedded pubkey vs the .pub of the key that will sign
    python3 scripts/check_updater_pubkey.py --pubkey-file ~/.conductloop-keys/conductloop.key.pub

    # after signing: the produced .sig must come from the embedded key
    python3 scripts/check_updater_pubkey.py --sig-file ConductLoop.app.tar.gz.sig

Stdlib only so it runs on a bare macOS python3 and in CI.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json"

_KEYID_RE = re.compile(r"^[0-9A-F]{16}$")
_COMMENT_KEYID_RE = re.compile(r"minisign public key:?\s*([0-9A-Fa-f]{16})")


class KeyCheckError(ValueError):
    """Raised for malformed key material or a key id mismatch."""


def _b64decode(value: str) -> bytes:
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeyCheckError(f"not valid base64: {exc}") from exc


def _minisign_text(value: str) -> str:
    """Return minisign text for a raw .pub/.sig or Tauri's base64-wrapped form."""
    value = value.strip()
    if not value:
        raise KeyCheckError("empty key material")
    if value.startswith("untrusted comment:"):
        return value
    try:
        decoded = _b64decode(value).decode("utf-8")
    except UnicodeDecodeError:
        decoded = ""
    if decoded.startswith("untrusted comment:"):
        return decoded
    # A bare minisign key/signature line (no comment).
    return "untrusted comment:\n" + value


def _keyid_from_blob(blob: bytes, kind: str) -> str:
    # minisign layout: 2-byte algorithm, 8-byte key id (little endian), payload.
    if len(blob) < 10 or blob[:2] not in (b"Ed", b"ED"):
        raise KeyCheckError(f"not a minisign {kind} (bad length/algorithm)")
    return blob[2:10][::-1].hex().upper()


def pubkey_keyid(value: str) -> str:
    """Key id of a minisign public key (raw .pub text or base64-wrapped)."""
    lines = _minisign_text(value).splitlines()
    if len(lines) < 2:
        raise KeyCheckError("public key has no key line")
    blob = _b64decode(lines[1])
    if len(blob) != 42 or blob[:2] != b"Ed":
        raise KeyCheckError("not a minisign Ed25519 public key (expected 42 bytes)")
    keyid = _keyid_from_blob(blob, "public key")
    m = _COMMENT_KEYID_RE.search(lines[0])
    if m and m.group(1).upper() != keyid:
        raise KeyCheckError(
            f"public key comment says {m.group(1).upper()} but the key bytes are {keyid}"
        )
    return keyid


def signature_keyid(value: str) -> str:
    """Key id that produced a minisign signature (raw .sig or base64-wrapped)."""
    lines = _minisign_text(value).splitlines()
    if len(lines) < 2:
        raise KeyCheckError("signature has no signature line")
    blob = _b64decode(lines[1])
    if len(blob) != 74:
        raise KeyCheckError("not a minisign signature (expected 74 bytes)")
    return _keyid_from_blob(blob, "signature")


def normalize_keyid(value: str) -> str:
    keyid = value.strip().upper()
    if not _KEYID_RE.match(keyid):
        raise KeyCheckError(f"invalid key id {value.strip()!r} (want 16 hex chars)")
    return keyid


def embedded_pubkey(config_path: Path) -> str:
    try:
        conf = json.loads(config_path.read_text(encoding="utf-8"))
        pubkey = conf["plugins"]["updater"]["pubkey"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise KeyCheckError(f"cannot read plugins.updater.pubkey from {config_path}: {exc}") from exc
    if not isinstance(pubkey, str) or not pubkey.strip():
        raise KeyCheckError(f"plugins.updater.pubkey is empty in {config_path}")
    return pubkey


def check(
    config_path: Path,
    expected: str | None = None,
    pubkey_text: str | None = None,
    sig_text: str | None = None,
) -> str:
    """Return the embedded key id; raise KeyCheckError on any mismatch."""
    if expected is None and pubkey_text is None and sig_text is None:
        raise KeyCheckError("nothing to compare against (pass --expected*, --pubkey-file or --sig-file)")
    embedded = pubkey_keyid(embedded_pubkey(config_path))
    comparisons: list[tuple[str, str]] = []
    if expected is not None:
        comparisons.append(("pinned key id", normalize_keyid(expected)))
    if pubkey_text is not None:
        comparisons.append(("signing .pub", pubkey_keyid(pubkey_text)))
    if sig_text is not None:
        comparisons.append(("signature", signature_keyid(sig_text)))
    for label, keyid in comparisons:
        if keyid != embedded:
            raise KeyCheckError(
                f"updater key mismatch: app embeds {embedded} but {label} is {keyid}. "
                "Installed apps would reject this update."
            )
    return embedded


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--expected", help="expected key id (16 hex chars)")
    p.add_argument("--expected-file", type=Path, help="file containing the expected key id")
    p.add_argument("--pubkey-file", type=Path, help="minisign/Tauri .pub of the signing key")
    p.add_argument("--sig-file", type=Path, help="minisign/Tauri .sig produced by signing")
    args = p.parse_args(argv)

    try:
        expected = args.expected
        if args.expected_file is not None:
            expected = args.expected_file.read_text(encoding="utf-8").strip()
        pubkey_text = args.pubkey_file.read_text(encoding="utf-8") if args.pubkey_file else None
        sig_text = args.sig_file.read_text(encoding="utf-8") if args.sig_file else None
        keyid = check(args.config, expected, pubkey_text, sig_text)
    except (KeyCheckError, OSError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ updater pubkey key id {keyid} matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
