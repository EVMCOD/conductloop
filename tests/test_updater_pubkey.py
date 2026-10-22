"""Updater signing-key consistency (scripts/check_updater_pubkey.py).

v1.1.1/v1.2.0 shipped an updater pubkey (D68774E13CB63DED) that did not match
the key that signs releases (770EA4C292DC6BD2), so in-app updates could never
verify. These tests pin the embedded key and the checker's behaviour.
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_updater_pubkey", ROOT / "scripts" / "check_updater_pubkey.py"
)
assert _spec and _spec.loader
cup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cup)

CONFIG = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
EXPECTED_FILE = ROOT / "desktop" / "UPDATER_KEY_ID"
RELEASE_SCRIPT = ROOT / "scripts" / "release-desktop.sh"

# Public signature of ConductLoop-1.2.0.app.tar.gz as published in
# EVMCOD/conductloop-releases (update-macos-aarch64.json, v1.2.0).
V120_SIG = "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUlVUU2E5eVN3cVFPZDREd1FpVHdTMkEyYy9TQURvUEZNVmNBQlU4TlJVSklmM05BanB1RnZHa3grYXZRYTROOHg0QU92UFExWHVXOGRsUVVDU1VPMFRVRm1PZGJxU3IxTXdVPQp0cnVzdGVkIGNvbW1lbnQ6IHRpbWVzdGFtcDoxNzg0ODI4NTM1CWZpbGU6Q29uZHVjdExvb3AuYXBwLnRhci5negpjeHg3SktjN1dCUk1wQXlRZXVQMUtzRFcrc255aGNzQjNORUZIK0FvUFdDWDQydFRXQWhnUjR5T1NvYU1XdU9XcDU4Y1MzNEVrNzNvTEJkRWI0ZFZCZz09Cg=="
# The pubkey v1.1.1/v1.2.0 shipped with (no matching private key exists).
OLD_PUBKEY = (
    "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IEQ2ODc3NEUxM0NCNjNERUQK"
    "UldUdFBiWTg0WFNIMW5JTjEydkIyaDk3bGovNUthckVQTkV0STc1OUJTVk5UazBhOU92RGE3aHcK"
)


def _fake_pub(keyid: str, comment_keyid: str | None = None) -> str:
    blob = b"Ed" + bytes.fromhex(keyid)[::-1] + bytes(32)
    line = base64.b64encode(blob).decode()
    return f"untrusted comment: minisign public key: {comment_keyid or keyid}\n{line}\n"


def _config_with(tmp_path: Path, pubkey: str) -> Path:
    conf = json.loads(CONFIG.read_text())
    conf["plugins"]["updater"]["pubkey"] = pubkey
    path = tmp_path / "tauri.conf.json"
    path.write_text(json.dumps(conf))
    return path


def test_embedded_pubkey_matches_pinned_key_id():
    expected = EXPECTED_FILE.read_text().strip()
    assert expected == "770EA4C292DC6BD2"
    assert cup.check(CONFIG, expected=expected) == expected


def test_embedded_pubkey_verifies_published_v120_signature_key():
    assert cup.signature_keyid(V120_SIG) == "770EA4C292DC6BD2"
    assert cup.check(CONFIG, sig_text=V120_SIG) == "770EA4C292DC6BD2"


def test_old_shipped_pubkey_is_rejected(tmp_path):
    cfg = _config_with(tmp_path, OLD_PUBKEY)
    assert cup.pubkey_keyid(OLD_PUBKEY) == "D68774E13CB63DED"
    with pytest.raises(cup.KeyCheckError, match="D68774E13CB63DED"):
        cup.check(cfg, expected="770EA4C292DC6BD2")
    with pytest.raises(cup.KeyCheckError, match="mismatch"):
        cup.check(cfg, sig_text=V120_SIG)


def test_pubkey_file_raw_and_tauri_base64_forms():
    raw = _fake_pub("770EA4C292DC6BD2")
    wrapped = base64.b64encode(raw.encode()).decode()
    assert cup.pubkey_keyid(raw) == "770EA4C292DC6BD2"
    assert cup.pubkey_keyid(wrapped) == "770EA4C292DC6BD2"
    assert cup.check(CONFIG, pubkey_text=wrapped) == "770EA4C292DC6BD2"
    with pytest.raises(cup.KeyCheckError, match="mismatch"):
        cup.check(CONFIG, pubkey_text=_fake_pub("F9F7C0E2E9DA9D4D"))


def test_tampered_comment_is_rejected():
    with pytest.raises(cup.KeyCheckError, match="comment"):
        cup.pubkey_keyid(_fake_pub("F9F7C0E2E9DA9D4D", comment_keyid="770EA4C292DC6BD2"))


@pytest.mark.parametrize("bad", ["", "not base64!!", "REPLACE_WITH_conductloop.key.pub"])
def test_placeholder_or_garbage_pubkey_fails(tmp_path, bad):
    cfg = _config_with(tmp_path, bad)
    with pytest.raises(cup.KeyCheckError):
        cup.check(cfg, expected="770EA4C292DC6BD2")


def test_invalid_expected_key_id():
    with pytest.raises(cup.KeyCheckError, match="invalid key id"):
        cup.check(CONFIG, expected="770E")


def test_cli_exit_codes(capsys):
    assert cup.main(["--expected-file", str(EXPECTED_FILE)]) == 0
    assert cup.main(["--expected", "D68774E13CB63DED"]) == 1
    assert "mismatch" in capsys.readouterr().err
    assert cup.main([]) == 1



def test_release_script_checks_key_before_building_and_after_signing():
    text = RELEASE_SCRIPT.read_text()
    assert 'KEY_CHECK="$(cd "$(dirname "$0")" && pwd)/check_updater_pubkey.py"' in text
    pre = text.index('--pubkey-file "$UPDATER_PUBKEY_FILE"')
    assert pre < text.index("cargo tauri build 2>&1")
    post = text.index("--sig-file ConductLoop.app.tar.gz.sig")
    assert text.index("cargo tauri signer sign ConductLoop") < post
    assert post < text.index('gh release create "$TAG"')
    # Only the public half is ever handed to the checker.
    assert 'UPDATER_PUBKEY_FILE="${UPDATER_PUBKEY_FILE:-$UPDATER_KEY.pub}"' in text


def test_release_notes_have_no_paid_plan_copy():
    text = RELEASE_SCRIPT.read_text()
    assert "free trial" not in text.lower()
    assert "/month" not in text
    assert "PUBKEY=" not in text  # unused, stale hard-coded pubkey removed
