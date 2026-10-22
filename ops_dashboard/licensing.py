"""ConductLoop licensing — free edition.

ConductLoop is **free for everyone** (since v1.1.0, 2026-07-16): no license
gate, no subscription, no trial, and (since 2026-10-21) no calls to the
license worker from the app. What remains is read-only compatibility:

  - `status()` always reports the free edition and never touches the network;
  - a license key left by a pre-free beta install is still read (keychain or
    legacy plaintext config.json) so `status()` can flag `legacy_key_on_file`.
    It is never sent anywhere and never deleted.
  - `enforce()` is kept, always False, for one release so callers can be
    removed gradually.
"""
from __future__ import annotations

import json

from . import app_paths as _app_paths

CONFIG_PATH = _app_paths.config_path()


def _load() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def enforce() -> bool:
    """Always False: ConductLoop has no license gate. Deprecated; kept one
    release for callers (OPS_LICENSE_ENFORCE is ignored)."""
    return False


def get_license() -> dict:
    """The legacy license key on file, if any (read-only)."""
    cfg = _load()
    from . import secrets_store
    # Prefer the keychain/store; fall back to a legacy plaintext config.json key.
    key = secrets_store.get_secret("license-key") or cfg.get("license_key")
    return {"key": key, "email": cfg.get("license_email")}


def _store_license(key: str, email: str, extra: dict | None = None) -> None:
    from . import secrets_store
    # The key is a secret → keychain/store, not plaintext config.json. Email and
    # any extra (plan, expiry) are not secret and stay in config for display.
    secrets_store.set_secret("license-key", key)
    cfg = _load()
    cfg.pop("license_key", None)  # scrub any legacy plaintext copy
    cfg["license_email"] = email
    if extra:
        cfg.update(extra)
    _save(cfg)


def status() -> dict:
    """Free edition, always. Never raises, never uses the network."""
    out = {
        "licensed": True,
        "enforced": False,
        "reason": "free_edition",
        "plan": "free",
        "edition": "free",
        "message": "ConductLoop is free. All features unlocked.",
    }
    try:
        if get_license().get("key"):
            out["legacy_key_on_file"] = True
    except Exception:
        pass
    return out
