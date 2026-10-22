"""The license key must live in the secret store (keychain/file), never in
plaintext config.json (upgrade #9)."""
from __future__ import annotations

import json

import pytest


@pytest.fixture()
def lic(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_DISABLE_KEYCHAIN", "1")
    monkeypatch.setenv("OPS_SECRETS_DIR", str(tmp_path / "secrets"))
    from ops_dashboard import licensing
    monkeypatch.setattr(licensing, "CONFIG_PATH", tmp_path / "config.json")
    return licensing


def test_key_stored_in_secret_store_not_config(lic, tmp_path):
    lic._store_license("CNDL-TEST-KEY", "user@example.com", extra={"plan": "free"})
    # round-trips through get_license
    got = lic.get_license()
    assert got["key"] == "CNDL-TEST-KEY"
    assert got["email"] == "user@example.com"
    # config.json holds email + plan but NOT the key
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["license_email"] == "user@example.com"
    assert cfg.get("plan") == "free"
    assert "license_key" not in cfg
    # the store has it
    from ops_dashboard import secrets_store
    assert secrets_store.get_secret("license-key") == "CNDL-TEST-KEY"


def test_legacy_plaintext_key_still_read(lic, tmp_path):
    """A pre-#9 install with the key in config.json still resolves."""
    (tmp_path / "config.json").write_text(json.dumps(
        {"license_key": "LEGACY-KEY", "license_email": "old@example.com"}))
    assert lic.get_license()["key"] == "LEGACY-KEY"
