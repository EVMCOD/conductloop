"""First-run frictions found walking a packaged install as a stranger."""
from __future__ import annotations

import json

import pytest

from ops_dashboard import brain, credentials, secret_vault


@pytest.fixture()
def locked_home(tmp_path, monkeypatch):
    """A fresh machine with no keychain and no open vault."""
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("OPS_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("OPS_DISABLE_KEYCHAIN", "1")
    monkeypatch.setenv("OPS_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(brain, "CONFIG_PATH", cfg)
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MINIMAX_API_KEY", "GATEWAY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    secret_vault.lock()
    return {"tmp": tmp_path, "cfg": cfg}


def test_ui_key_save_refuses_plaintext_when_vault_locked(locked_home):
    with pytest.raises(secret_vault.VaultLocked):
        credentials.set_secret("byok.anthropic.default", "sk-ant-test-key")
    assert not locked_home["cfg"].exists() or "anthropic_api_key" not in locked_home["cfg"].read_text()


def test_ui_key_save_goes_to_encrypted_vault_after_passphrase(locked_home):
    from ops_dashboard import byok_keys

    assert secret_vault.unlock("stranger-passphrase") is True
    out = credentials.set_secret("byok.anthropic.default", "sk-ant-test-key")
    assert out["where"] in ("encrypted", "keychain")
    cfg = json.loads(locked_home["cfg"].read_text())
    assert "anthropic_api_key" not in cfg
    assert cfg.get("anthropic_api_key_ref")
    assert secret_vault.get("byok.anthropic.default") == "sk-ant-test-key"
    # The brain must resolve the same key it just saved (Codex P1 on #101).
    assert byok_keys.get_api_key("anthropic", cfg) == "sk-ant-test-key"
    assert byok_keys.key_storage("anthropic", cfg) == "vault"


def test_mock_points_at_related_notes_instead_of_nothing_matches():
    docs = [{
        "source": "obsidian", "path": "/v/pricing.md", "title": "Pricing",
        "content": "# Pricing\n\nOur plan costs $29 per month for teams.\n",
    }]
    context, _ = brain._format_context(docs)
    system = (brain.SYSTEM_PROMPT_BASE.format(today="2026-11-05", preferences="(none yet)")
              + "\n\nRelevant context (cite with [N]):\n" + context)
    out = brain.MockProvider().ask(system, [{"role": "user", "content": "What does our pricing say?"}])
    assert "nothing in your notes matches" not in out
    assert "**Pricing** [1]" in out
    assert "welcome page" in out or "Credentials" in out
    assert "BRAIN_PROVIDER" not in out


def test_provider_errors_are_english_and_point_at_credentials():
    err = brain._classify_provider_error(Exception("invalid_api_key"), "anthropic")
    assert err.code == "invalid_key"
    assert "Credentials" in err.message
    assert "Settings" not in err.message
    assert "El provider" not in err.message
