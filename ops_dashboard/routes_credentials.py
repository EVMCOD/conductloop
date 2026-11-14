"""The credentials of the app: what it holds, and where it lives.

GET    /api/credentials            the inventory — never a value
POST   /api/credentials/unlock     open the encrypted vault with a passphrase
POST   /api/credentials/migrate    move the plain-text keys into the vault
POST   /api/credentials/<name>     put a key in
DELETE /api/credentials/<name>     take a key out

The specific routes are registered before the generic one: the router
takes the first match.
"""
from __future__ import annotations

import json

from . import credentials, secret_vault
from .routing import router


def _send(handler, code: int, obj: dict) -> None:
    handler._send(code, json.dumps(obj).encode(), "application/json")


def _err(handler, code: int, error: str, message: str) -> None:
    _send(handler, code, {"ok": False, "error": error, "message": message})


def _body(handler) -> dict:
    try:
        data = handler._read_json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


@router.get("/api/credentials")
def credentials_inventory(handler) -> None:
    """Every credential of the app, and where it lives.

    No value ever comes back: only the place where it is.
    """
    try:
        _send(handler, 200, {"ok": True, **credentials.inventory()})
    except Exception as e:
        _err(handler, 500, "inventory_failed", str(e))


@router.post("/api/credentials/unlock")
def credentials_unlock(handler) -> None:
    """Open the encrypted vault with the passphrase of the user.

    The passphrase is never stored: it is asked for again next time.
    """
    data = _body(handler)
    passphrase = str(data.get("passphrase") or "")
    if not passphrase:
        _err(handler, 400, "bad_request", "a passphrase is needed to open the vault")
        return
    if not secret_vault.unlock(passphrase):
        _err(handler, 401, "wrong_passphrase",
             "that passphrase does not open the vault")
        return
    _send(handler, 200, {"ok": True, **credentials.inventory()})


@router.post("/api/credentials/migrate")
def credentials_migrate(handler) -> None:
    """Move the keys that are in plain text into the vault."""
    try:
        result = credentials.migrate_plaintext()
    except Exception as e:
        _err(handler, 500, "migration_failed", str(e))
        return
    _send(handler, 200, {"ok": True, **result, **credentials.inventory()})


@router.post("/api/credentials/<name>")
def credentials_set(handler, name: str) -> None:
    """Put a key in.

    The value goes to the keychain (or to the encrypted vault) and is never
    stored in the database and never sent back.
    """
    data = _body(handler)
    value = str(data.get("value") or "")
    try:
        result = credentials.set_secret(name, value)
    except ValueError as e:
        _err(handler, 400, "bad_request", str(e))
        return
    except secret_vault.VaultLocked as e:
        _err(handler, 423, "vault_locked", str(e))
        return
    except Exception as e:
        _err(handler, 500, "could_not_store", str(e))
        return
    _send(handler, 200, {"ok": True, **result, **credentials.inventory()})


@router.delete("/api/credentials/<name>")
def credentials_delete(handler, name: str) -> None:
    """Take a key out."""
    try:
        result = credentials.delete_secret(name)
    except Exception as e:
        _err(handler, 500, "could_not_remove", str(e))
        return
    _send(handler, 200, {"ok": True, **result, **credentials.inventory()})
