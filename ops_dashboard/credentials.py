"""Every credential the app holds, and where it lives.

A person who downloads the app should be able to see what the app has
their keys for, put a new one in and take one out — without opening a
terminal, and without the app ever handing a key back.

The inventory never contains a value. It says *where* a credential is:

* ``keychain`` — the OS keychain: nothing of it is on this disk.
* ``encrypted`` — an encrypted file whose key comes from the passphrase
  the user typed in the app.
* ``plaintext`` — in ``config.json``, in the open. The one to fix, and
  the migration is what fixes it.
* ``nowhere`` — not set up.
"""
from __future__ import annotations

import logging
from typing import Any

from . import secret_vault

log = logging.getLogger("conductloop.credentials")

BRAIN_LABELS = {
    "minimax": "MiniMax — the brain",
    "anthropic": "Anthropic — the brain",
    "openai": "OpenAI-compatible — the brain",
    "gateway": "Gateway (Vercel AI Gateway / OpenRouter)",
}

GROUPS = {
    "brain": "The brain — what it thinks with",
    "media": "Media — images, video and speech",
    "connectors": "Connectors — Gmail, GitHub, Linear…",
}


def _brain_rows() -> list[dict[str, Any]]:
    """The keys of the brain, and where each one lives."""
    from . import brain, byok_keys

    cfg = brain._load_config()
    state = byok_keys.status(cfg)
    out: list[dict[str, Any]] = []
    for provider in byok_keys.PROVIDERS:
        name = secret_vault.brain_key_name(provider)
        if provider in state.get("plaintext_providers") or []:
            where = "plaintext"
        else:
            where = secret_vault.where(name)
        out.append({
            "name": name,
            "group": "brain",
            "provider": provider,
            "label": BRAIN_LABELS.get(provider, provider),
            "where": where,
            "editable": True,
        })
    return out


def _media_rows() -> list[dict[str, Any]]:
    """The keys of the media providers."""
    from . import media

    media.providers._ensure_backends()
    out: list[dict[str, Any]] = []
    for spec in media.providers.PROVIDER_SPECS:
        if not spec.needs_key:
            continue
        name = secret_vault.media_key_name(spec.key)
        out.append({
            "name": name,
            "group": "media",
            "provider": spec.key,
            "label": spec.label,
            "where": secret_vault.where(name),
            "editable": True,
        })
    return out


def _connector_rows() -> list[dict[str, Any]]:
    """The tokens of the connectors.

    These are already keychain-only and they carry their own expiry and
    refresh, so the panel reports them and does not touch them.
    """
    try:
        from . import connector_registry
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for spec in connector_registry.all_specs():
        item = f"connector-token:{spec.id}"
        where = secret_vault.where(item)
        secret = f"connector-client-secret:{spec.id}"
        if secret_vault.where(secret) != "nowhere":
            where = "keychain" if where != "nowhere" else where
        out.append({
            "name": item,
            "group": "connectors",
            "provider": spec.id,
            "label": spec.name,
            "where": where,
            "editable": False,
        })
    return out


def inventory() -> dict[str, Any]:
    """Every credential, where it lives, and how the vault itself is doing.

    Never a value.
    """
    rows = _brain_rows() + _media_rows() + _connector_rows()
    return {
        "rows": rows,
        "groups": GROUPS,
        "backend": secret_vault.backend(),
        "keychain": secret_vault.keychain_available(),
        "open": secret_vault.is_open(),
        "passphrase": secret_vault.passphrase_set(),
        "stored": sum(1 for r in rows if r["where"] in ("keychain", "encrypted")),
        "plaintext": [r["label"] for r in rows if r["where"] == "plaintext"],
        "total": len(rows),
    }


def set_secret(name: str, value: str) -> dict[str, Any]:
    """Put a key in, wherever it belongs for that kind of credential.

    The brain's keys and the media providers' keys each have their own
    way of being stored (one in the configuration, the other in the
    database), and both end up in the vault.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("a key cannot be empty")
    if name.startswith("byok."):
        from . import brain

        provider = name.split(".")[1] if len(name.split(".")) > 1 else ""
        if not provider:
            raise ValueError(f"'{name}' is not a credential of the app")
        brain.set_provider_key(provider, api_key=value)
        return {"name": name, "where": secret_vault.where(name)}
    if name.startswith("media."):
        from . import media

        provider = name[len("media."):-len(".api_key")] if name.endswith(".api_key") else ""
        if not provider:
            raise ValueError(f"'{name}' is not a credential of the app")
        media.providers.save_config(provider, {"api_key": value})
        return {"name": name, "where": secret_vault.where(name)}
    raise ValueError(f"'{name}' is not a credential of the app")


def delete_secret(name: str) -> dict[str, Any]:
    """Take a key out.

    The brain's keys also leave the configuration: a key that was deleted
    from the vault but stayed in ``config.json`` would still be there.
    """
    from . import brain, byok_keys

    if name.startswith("byok."):
        provider = name.split(".")[1] if len(name.split(".")) > 1 else ""
        cfg = brain._load_config()
        if provider:
            cfg.pop(byok_keys.PLAINTEXT_FIELD.get(provider, ""), None)
            cfg.pop(byok_keys.REF_FIELD.get(provider, ""), None)
            brain._save_config(cfg)
    secret_vault.delete(name)
    return {"name": name, "where": secret_vault.where(name)}


def migrate_plaintext() -> dict[str, Any]:
    """Move the keys that are lying in plain text into the vault.

    Two places hold a key in the open: the media providers' database and
    the brain's ``config.json``. Both are moved, and both are left empty
    behind.
    """
    from . import brain, byok_keys, media

    moved: list[str] = []
    failed: list[str] = []

    # 1. The media providers' database.
    try:
        result = media.providers.migrate_plaintext_keys()
        moved.extend(result.get("moved") or [])
        failed.extend(result.get("failed") or [])
    except Exception as e:
        log.warning("could not move the media keys into the vault: %s", e)
        failed.append("media")

    # 2. The brain's config.json.
    cfg = brain._load_config()
    for provider in byok_keys.PROVIDERS:
        value = str(cfg.get(byok_keys.PLAINTEXT_FIELD[provider]) or "").strip()
        if not value:
            continue
        try:
            secret_vault.set(secret_vault.brain_key_name(provider), value)
        except Exception as e:
            log.warning("could not move the key of %s: %s", provider, e)
            failed.append(provider)
            continue
        cfg.pop(byok_keys.PLAINTEXT_FIELD[provider], None)
        cfg[byok_keys.REF_FIELD[provider]] = secret_vault.brain_key_name(provider)
        moved.append(provider)

    if cfg != brain._load_config():
        brain._save_config(cfg)

    return {"moved": moved, "failed": failed}
