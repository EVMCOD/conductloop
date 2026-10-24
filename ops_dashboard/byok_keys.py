"""BYOK provider API keys: OS keychain (via secrets_store) with a config ref.

Storage model
-------------
* Keychain available → the key lives in the OS keychain under
  ``byok.<provider>.default`` and ``config.json`` keeps only
  ``<provider>_api_key_ref`` (the item name, never the value).
* Keychain unavailable → historic behaviour: plaintext ``<provider>_api_key``
  in ``config.json`` (0600). The UI warns about it.

Reads stay backward compatible: env var → keychain ref → legacy plaintext.

Existing plaintext keys are moved ONLY with the user's explicit consent
(:func:`migrate_to_keychain`); each key is written, read back and compared
before its plaintext copy is removed. :func:`revert_to_config` undoes it.

Nothing here logs, raises or returns a key value.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from . import secrets_store

log = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "openai", "minimax", "gateway")
ENV_VAR = {p: f"{p.upper()}_API_KEY" for p in PROVIDERS}
PLAINTEXT_FIELD = {p: f"{p}_api_key" for p in PROVIDERS}
REF_FIELD = {p: f"{p}_api_key_ref" for p in PROVIDERS}
PROMPT_FIELD = "byok_keychain_prompt"   # "dismissed" once the user says "not now"

_CACHE_TTL = 60.0
_cache: dict[str, tuple[float, str | None]] = {}
_cache_lock = threading.Lock()


def default_ref(provider: str) -> str:
    return f"byok.{provider}.default"


def _check(provider: str) -> str:
    provider = (provider or "").lower().strip()
    if provider not in PROVIDERS:
        raise ValueError(f"provider has no API key: {provider}")
    return provider


def _keychain_read(ref: str) -> str | None:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(ref)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    value = secrets_store.keychain_get(ref)
    with _cache_lock:
        _cache[ref] = (now, value)
    return value


def _invalidate(ref: str | None = None) -> None:
    with _cache_lock:
        if ref is None:
            _cache.clear()
        else:
            _cache.pop(ref, None)


def get_api_key(provider: str, cfg: dict) -> str:
    """Resolve a provider key: env var, then keychain ref, then legacy plaintext."""
    provider = _check(provider)
    env = os.environ.get(ENV_VAR[provider])
    if env:
        return env
    ref = cfg.get(REF_FIELD[provider])
    if ref:
        value = _keychain_read(ref)
        if value:
            return value
        log.warning("BYOK key for %s not found in keychain (ref %s)", provider, ref)
    return cfg.get(PLAINTEXT_FIELD[provider], "") or ""


def key_storage(provider: str, cfg: dict) -> str | None:
    """Where the effective key comes from: env | keychain | config | None."""
    provider = _check(provider)
    if os.environ.get(ENV_VAR[provider]):
        return "env"
    if cfg.get(REF_FIELD[provider]) and _keychain_read(cfg[REF_FIELD[provider]]):
        return "keychain"
    if cfg.get(PLAINTEXT_FIELD[provider]):
        return "config"
    return None


def store_api_key(provider: str, api_key: str, cfg: dict) -> str:
    """Store a newly entered key; mutates ``cfg`` (caller saves it).

    Returns the storage used: "keychain", "vault" (an encrypted file, for
    a machine with no keychain), "config" (plaintext — the last resort, and
    the one the Credentials page points at) or "cleared" (empty key)."""
    provider = _check(provider)
    value = (api_key or "").strip()
    if not value:
        cfg.pop(PLAINTEXT_FIELD[provider], None)
        cfg.pop(REF_FIELD[provider], None)
        return "cleared"
    ref = default_ref(provider)
    if secrets_store.keychain_set(ref, value):
        _invalidate(ref)
        if secrets_store.keychain_get(ref) == value:
            cfg[REF_FIELD[provider]] = ref
            cfg.pop(PLAINTEXT_FIELD[provider], None)
            return "keychain"
        log.warning("BYOK key for %s did not verify after keychain write; keeping config.json",
                    provider)
    # No keychain on this machine: the encrypted vault, when it is open.
    try:
        from . import secret_vault

        secret_vault.set(ref, value)
        _invalidate(ref)
        if secret_vault.get(ref) == value:
            cfg[REF_FIELD[provider]] = ref
            cfg.pop(PLAINTEXT_FIELD[provider], None)
            return "vault"
    except Exception as e:
        log.warning("the key of %s could not go into the vault: %s", provider, e)
    # Last resort: plain text in config.json, on this machine only. The
    # Credentials page says so out loud and the migration offers to move it.
    cfg[PLAINTEXT_FIELD[provider]] = value
    cfg.pop(REF_FIELD[provider], None)
    return "config"


def status(cfg: dict) -> dict:
    """Migration banner state. Never includes key values."""
    plaintext = [p for p in PROVIDERS if cfg.get(PLAINTEXT_FIELD[p])]
    in_keychain = [p for p in PROVIDERS if cfg.get(REF_FIELD[p])]
    available = secrets_store.keychain_available()
    dismissed = cfg.get(PROMPT_FIELD) == "dismissed"
    return {
        "keychain_available": available,
        "plaintext_providers": plaintext,
        "keychain_providers": in_keychain,
        "dismissed": dismissed,
        "should_prompt": bool(plaintext) and not dismissed,
    }


def migrate_to_keychain(cfg: dict, *, consent: bool,
                        providers: list[str] | None = None) -> dict:
    """Move plaintext keys into the keychain. Requires ``consent=True``.

    Mutates ``cfg`` only for keys that were written AND read back identically;
    anything else stays exactly where it was. Caller saves ``cfg``."""
    if consent is not True:
        raise PermissionError("explicit user consent required to move keys")
    if not secrets_store.keychain_available():
        return {"ok": False, "reason": "keychain_unavailable", "moved": [], "failed": []}
    wanted = [_check(p) for p in providers] if providers else list(PROVIDERS)
    moved: list[str] = []
    failed: list[str] = []
    for p in wanted:
        value = cfg.get(PLAINTEXT_FIELD[p])
        if not value:
            continue
        ref = default_ref(p)
        ok = secrets_store.keychain_set(ref, value)
        _invalidate(ref)
        if ok and secrets_store.keychain_get(ref) == value:
            cfg[REF_FIELD[p]] = ref
            del cfg[PLAINTEXT_FIELD[p]]
            moved.append(p)
        else:
            failed.append(p)
    if moved:
        cfg.pop(PROMPT_FIELD, None)
    log.info("BYOK keychain migration: moved=%s failed=%s", moved, failed)
    return {"ok": not failed, "moved": moved, "failed": failed}


def revert_to_config(cfg: dict, *, consent: bool,
                     providers: list[str] | None = None) -> dict:
    """Undo a migration: copy keychain keys back into config.json plaintext.

    The keychain items are left in place (harmless; removable in Keychain
    Access). Requires ``consent=True``. Caller saves ``cfg``."""
    if consent is not True:
        raise PermissionError("explicit user consent required")
    wanted = [_check(p) for p in providers] if providers else list(PROVIDERS)
    restored: list[str] = []
    failed: list[str] = []
    for p in wanted:
        ref = cfg.get(REF_FIELD[p])
        if not ref:
            continue
        _invalidate(ref)
        value = secrets_store.keychain_get(ref)
        if value:
            cfg[PLAINTEXT_FIELD[p]] = value
            del cfg[REF_FIELD[p]]
            restored.append(p)
        else:
            failed.append(p)
    return {"ok": not failed, "restored": restored, "failed": failed}


def dismiss_prompt(cfg: dict) -> None:
    cfg[PROMPT_FIELD] = "dismissed"
