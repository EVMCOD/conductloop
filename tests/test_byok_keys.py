"""BYOK keys → OS keychain with consent (multi-agent PR 2).

Uses an in-memory fake keychain (patched into secrets_store); never touches a
real keychain and uses only obviously fake key strings.
"""
from __future__ import annotations

import json
import logging

import pytest

from ops_dashboard import brain, byok_keys, secrets_store

FAKE = "sk-fake-test-value-0000"
FAKE2 = "sk-fake-other-value-1111"


class FakeKeychain:
    def __init__(self, enabled: bool = True, corrupt_reads: bool = False):
        self.enabled = enabled
        self.corrupt_reads = corrupt_reads
        self.items: dict[str, str] = {}
        self.reads = 0

    def install(self, monkeypatch):
        monkeypatch.setattr(secrets_store, "_keychain_enabled", lambda: self.enabled)
        monkeypatch.setattr(secrets_store, "_kc_get", self._get)
        monkeypatch.setattr(secrets_store, "_kc_set", self._set)
        return self

    def _get(self, name):
        self.reads += 1
        v = self.items.get(name)
        return (v + "x") if (v is not None and self.corrupt_reads) else v

    def _set(self, name, value):
        self.items[name] = value
        return True


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for p in byok_keys.PROVIDERS:
        monkeypatch.delenv(byok_keys.ENV_VAR[p], raising=False)
    monkeypatch.setenv("OPS_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr(brain, "CONFIG_PATH", tmp_path / "config.json")
    byok_keys._invalidate()
    yield
    byok_keys._invalidate()


def _write_cfg(cfg: dict) -> None:
    brain._save_config(cfg)


def _cfg() -> dict:
    return json.loads(brain.CONFIG_PATH.read_text())


# ── reads: env > keychain ref > legacy plaintext ─────────────────────────────

def test_legacy_plaintext_key_still_used(monkeypatch):
    FakeKeychain().install(monkeypatch)
    _write_cfg({"openai_api_key": FAKE})
    assert brain.resolve_provider("openai").api_key == FAKE
    assert byok_keys.key_storage("openai", _cfg()) == "config"


def test_env_var_wins(monkeypatch):
    FakeKeychain().install(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE2)
    _write_cfg({"openai_api_key": FAKE})
    assert brain.resolve_provider("openai").api_key == FAKE2


def test_keychain_ref_is_read_and_cached(monkeypatch):
    kc = FakeKeychain().install(monkeypatch)
    kc.items["byok.anthropic.default"] = FAKE
    _write_cfg({"anthropic_api_key_ref": "byok.anthropic.default"})
    for _ in range(5):
        assert brain.resolve_provider("anthropic").api_key == FAKE
    assert kc.reads == 1  # TTL cache: no subprocess per resolution


# ── new keys ─────────────────────────────────────────────────────────────────

def test_new_key_goes_to_keychain_and_config_keeps_only_ref(monkeypatch):
    kc = FakeKeychain().install(monkeypatch)
    status = brain.set_provider_key("openai", api_key=f"  {FAKE}  ")
    cfg = _cfg()
    assert kc.items == {"byok.openai.default": FAKE}
    assert cfg == {"openai_api_key_ref": "byok.openai.default"}
    assert FAKE not in brain.CONFIG_PATH.read_text()
    assert FAKE not in json.dumps(status)
    assert next(s for s in status if s["name"] == "openai")["key_storage"] == "keychain"


def test_new_key_without_keychain_goes_to_the_encrypted_vault(monkeypatch):
    """Without a keychain the key is not left in plain text.

    It used to stay in ``config.json`` where anybody who could read the file
    could read the key. Now it goes into the vault: an encrypted file whose
    key comes from the passphrase the user typed in the app.
    """
    from ops_dashboard import secret_vault

    FakeKeychain(enabled=False).install(monkeypatch)
    brain.set_provider_key("openai", api_key=FAKE)
    assert _cfg() == {"openai_api_key_ref": "byok.openai.default"}
    assert FAKE not in brain.CONFIG_PATH.read_text()
    assert byok_keys.status(_cfg())["keychain_available"] is False
    # And it really is in the vault.
    assert secret_vault.get("byok.openai.default") == FAKE


def test_unverified_keychain_write_falls_back_to_config(monkeypatch):
    FakeKeychain(corrupt_reads=True).install(monkeypatch)
    brain.set_provider_key("openai", api_key=FAKE)
    assert _cfg() == {"openai_api_key": FAKE}


def test_empty_key_clears_both(monkeypatch):
    FakeKeychain().install(monkeypatch)
    _write_cfg({"openai_api_key": FAKE, "openai_api_key_ref": "byok.openai.default"})
    brain.set_provider_key("openai", api_key="")
    assert _cfg() == {}


@pytest.mark.posix_perms
def test_config_written_owner_only(monkeypatch):
    FakeKeychain(enabled=False).install(monkeypatch)
    brain.set_provider_key("openai", api_key=FAKE)
    assert (brain.CONFIG_PATH.stat().st_mode & 0o777) == 0o600


# ── consented migration ──────────────────────────────────────────────────────

def test_status_prompts_for_plaintext_keys_without_values(monkeypatch):
    FakeKeychain().install(monkeypatch)
    st = byok_keys.status({"openai_api_key": FAKE, "minimax_api_key": FAKE2})
    assert st == {"keychain_available": True, "plaintext_providers": ["openai", "minimax"],
                  "keychain_providers": [], "dismissed": False, "should_prompt": True}
    assert FAKE not in json.dumps(st)


def test_migration_requires_consent(monkeypatch):
    kc = FakeKeychain().install(monkeypatch)
    cfg = {"openai_api_key": FAKE}
    for bad in (False, None, "yes", 1):
        with pytest.raises(PermissionError):
            byok_keys.migrate_to_keychain(cfg, consent=bad)  # type: ignore[arg-type]
    assert cfg == {"openai_api_key": FAKE} and kc.items == {}


def test_migration_moves_verified_keys_only(monkeypatch):
    kc = FakeKeychain().install(monkeypatch)
    cfg = {"openai_api_key": FAKE, "anthropic_api_key": FAKE2, "openai_model": "m",
           byok_keys.PROMPT_FIELD: "dismissed"}
    res = byok_keys.migrate_to_keychain(cfg, consent=True)
    assert res == {"ok": True, "moved": ["anthropic", "openai"], "failed": []}
    assert cfg == {"openai_api_key_ref": "byok.openai.default",
                   "anthropic_api_key_ref": "byok.anthropic.default", "openai_model": "m"}
    assert kc.items == {"byok.openai.default": FAKE, "byok.anthropic.default": FAKE2}
    assert byok_keys.get_api_key("openai", cfg) == FAKE


def test_migration_failure_leaves_plaintext_untouched(monkeypatch):
    FakeKeychain(corrupt_reads=True).install(monkeypatch)
    cfg = {"openai_api_key": FAKE}
    res = byok_keys.migrate_to_keychain(cfg, consent=True)
    assert res == {"ok": False, "moved": [], "failed": ["openai"]}
    assert cfg == {"openai_api_key": FAKE}


def test_migration_without_keychain_is_a_noop(monkeypatch):
    FakeKeychain(enabled=False).install(monkeypatch)
    cfg = {"openai_api_key": FAKE}
    res = byok_keys.migrate_to_keychain(cfg, consent=True)
    assert res["reason"] == "keychain_unavailable" and cfg == {"openai_api_key": FAKE}


def test_revert_restores_plaintext(monkeypatch):
    FakeKeychain().install(monkeypatch)
    cfg = {"openai_api_key": FAKE}
    byok_keys.migrate_to_keychain(cfg, consent=True)
    res = byok_keys.revert_to_config(cfg, consent=True)
    assert res == {"ok": True, "restored": ["openai"], "failed": []}
    assert cfg == {"openai_api_key": FAKE}
    with pytest.raises(PermissionError):
        byok_keys.revert_to_config(cfg, consent=False)


def test_dismiss_stops_prompt(monkeypatch):
    FakeKeychain().install(monkeypatch)
    cfg = {"openai_api_key": FAKE}
    byok_keys.dismiss_prompt(cfg)
    assert byok_keys.status(cfg)["should_prompt"] is False
    assert cfg["openai_api_key"] == FAKE  # declining never touches keys


def test_keychain_set_never_falls_back_to_file(monkeypatch, tmp_path):
    FakeKeychain(enabled=False).install(monkeypatch)
    assert secrets_store.keychain_set("byok.openai.default", FAKE) is False
    assert not (tmp_path / "secrets").exists()


def test_values_never_logged(monkeypatch, caplog):
    FakeKeychain(corrupt_reads=True).install(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        brain.set_provider_key("openai", api_key=FAKE)
        byok_keys.migrate_to_keychain({"anthropic_api_key": FAKE2}, consent=True)
        byok_keys.get_api_key("gateway", {"gateway_api_key_ref": "byok.gateway.default"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert FAKE not in text and FAKE2 not in text


# ── routes (registry) ────────────────────────────────────────────────────────

class _H:
    def __init__(self, body: dict | None = None, local: bool = True):
        self._body = body or {}
        self._local = local
        self.path = "/"
        self.sent: tuple = ()

    def _is_local(self):
        return self._local

    def _read_json(self):
        return self._body

    def _send(self, code, body, ctype):
        self.sent = (code, json.loads(body))


def _call(method, path, **kw):
    from ops_dashboard import routes_meta  # noqa: F401  registers routes
    from ops_dashboard.routing import router
    h = _H(**kw)
    assert router.dispatch(h, method, path)
    return h.sent


def test_routes_registered():
    from ops_dashboard import routes_meta  # noqa: F401
    from ops_dashboard.routing import router
    for m, p in [("GET", "/api/brain/keys/status"), ("POST", "/api/brain/keys/migrate"),
                 ("POST", "/api/brain/keys/revert"), ("POST", "/api/brain/keys/dismiss")]:
        assert (m, p) in router.registered()


def test_route_flow_status_migrate_revert(monkeypatch):
    kc = FakeKeychain().install(monkeypatch)
    _write_cfg({"openai_api_key": FAKE})
    code, body = _call("GET", "/api/brain/keys/status")
    assert code == 200 and body["should_prompt"] and FAKE not in json.dumps(body)

    code, body = _call("POST", "/api/brain/keys/migrate", body={"consent": False})
    assert code == 400 and _cfg() == {"openai_api_key": FAKE}

    code, body = _call("POST", "/api/brain/keys/migrate", body={"consent": True})
    assert code == 200 and body["moved"] == ["openai"] and FAKE not in json.dumps(body)
    assert _cfg() == {"openai_api_key_ref": "byok.openai.default"}
    assert kc.items["byok.openai.default"] == FAKE

    code, body = _call("POST", "/api/brain/keys/revert", body={"consent": True})
    assert code == 200 and body["restored"] == ["openai"] and FAKE not in json.dumps(body)
    assert _cfg() == {"openai_api_key": FAKE}


def test_routes_mutations_are_local_only(monkeypatch):
    FakeKeychain().install(monkeypatch)
    _write_cfg({"openai_api_key": FAKE})
    for path in ("/api/brain/keys/migrate", "/api/brain/keys/revert", "/api/brain/keys/dismiss"):
        code, _ = _call("POST", path, body={"consent": True}, local=False)
        assert code == 403
    assert _cfg() == {"openai_api_key": FAKE}


def test_route_dismiss(monkeypatch):
    FakeKeychain().install(monkeypatch)
    _write_cfg({"openai_api_key": FAKE})
    code, body = _call("POST", "/api/brain/keys/dismiss")
    assert code == 200 and body["status"]["should_prompt"] is False
    assert _cfg()["openai_api_key"] == FAKE


def test_route_bad_providers(monkeypatch):
    FakeKeychain().install(monkeypatch)
    code, _ = _call("POST", "/api/brain/keys/migrate", body={"consent": True, "providers": "openai"})
    assert code == 400
    code, _ = _call("POST", "/api/brain/keys/migrate", body={"consent": True, "providers": ["nope"]})
    assert code == 400


def test_banner_wired_minimally_into_index():
    from pathlib import Path
    web = Path(brain.__file__).parent / "web"
    html = (web / "index.html").read_text()
    assert html.count('<script src="byok-banner.js"></script>') == 1
    js = (web / "byok-banner.js").read_text()
    # Migration is only ever requested with explicit consent from a click.
    assert js.count("/api/brain/keys/migrate") == 1 and "consent: true" in js
    assert "innerHTML" not in js  # text only, no HTML injection
