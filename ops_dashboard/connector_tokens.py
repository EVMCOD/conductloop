"""OAuth tokens and BYOK client secrets for connectors — **OS keychain only**.

Unlike ``secrets_store.set_secret`` (which falls back to a 0600 file), connector
tokens must never land on disk in plaintext. If no keychain is available the
store refuses (``TokenStoreUnavailable``) and the connector stays disconnected.

Backends (``backend()``):
  * ``_KeychainBackend`` — the macOS login Keychain or, on Linux, the Secret
    Service, both through ``secrets_store``.
    - With #16 merged it uses ``secrets_store.keychain_available/get/set``.
    - With #17 merged writes go through ``secrets_store._kc_write`` (value on
      stdin, never argv). On a base without #17 the ``security`` CLI would get
      the token in argv (visible to ``ps``), so **writes are refused** until
      #17 lands; reads/deletes only pass the item name and are always safe.
    This is the "minimal shim" pattern from #22: when #16/#17 land the shim
    branches become dead code; nothing here changes their behaviour.
  * ``MemoryBackend`` — in-process dict, used by tests (``use_backend``).

Item names: ``connector-token:<id>`` (JSON: access/refresh token, expiry, scope)
and ``connector-client-secret:<id>`` (BYOK client secret, optional).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

from . import secrets_store

log = logging.getLogger("conductloop.connectors.tokens")


class TokenStoreUnavailable(RuntimeError):
    """No safe keychain backend: tokens are never written to a file instead."""


class Backend(Protocol):
    name: str

    def available(self) -> bool: ...
    def get(self, item: str) -> str | None: ...
    def set(self, item: str, value: str) -> None: ...
    def delete(self, item: str) -> None: ...


class _KeychainBackend:
    name = "keychain"

    def available(self) -> bool:
        fn = getattr(secrets_store, "keychain_available", None)          # #16
        ok = bool(fn()) if callable(fn) else secrets_store._keychain_enabled()
        return ok and self._writer() is not None

    @staticmethod
    def _writer() -> Any:
        # #17: argv-free writer. Without it the token would be visible in `ps`.
        return getattr(secrets_store, "_kc_write", None)

    def get(self, item: str) -> str | None:
        fn = getattr(secrets_store, "keychain_get", None)                 # #16
        if callable(fn):
            return fn(item)
        return secrets_store._kc_get(item) if secrets_store._keychain_enabled() else None

    def set(self, item: str, value: str) -> None:
        writer = self._writer()
        if not self.available() or writer is None:
            raise TokenStoreUnavailable("keychain_unavailable")
        if not writer(item, value):
            raise TokenStoreUnavailable("keychain_write_failed")

    def delete(self, item: str) -> None:
        # ``_kc_delete`` passes only the item name, on every platform (the macOS
        # ``security`` call and the Linux Secret Service live in secrets_store).
        fn = getattr(secrets_store, "_kc_delete", None)
        if callable(fn):
            fn(item)
        # The delete helpers swallow timeouts/denials: confirm the item is gone
        # so "disconnected" is never reported while the token is still stored.
        if self.available() and self.get(item) is not None:
            raise TokenStoreUnavailable("keychain_delete_failed")


class MemoryBackend:
    """Test backend. Never used unless installed with ``use_backend``."""
    name = "memory"

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def available(self) -> bool:
        return True

    def get(self, item: str) -> str | None:
        return self.items.get(item)

    def set(self, item: str, value: str) -> None:
        self.items[item] = value

    def delete(self, item: str) -> None:
        self.items.pop(item, None)


_backend: Backend = _KeychainBackend()


def backend() -> Backend:
    return _backend


def use_backend(b: Backend) -> Backend:
    """Swap the backend (tests). Returns the previous one."""
    global _backend
    prev, _backend = _backend, b
    return prev


def _token_item(connector_id: str) -> str:
    return f"connector-token:{connector_id}"


def _secret_item(connector_id: str) -> str:
    return f"connector-client-secret:{connector_id}"


def available() -> bool:
    try:
        return _backend.available()
    except Exception:
        return False


def save_token(connector_id: str, token: dict, *, now: float | None = None) -> dict:
    """Persist the token response; returns the stored record (with expires_at)."""
    now = time.time() if now is None else now
    rec = {
        "access_token": str(token.get("access_token") or ""),
        "refresh_token": token.get("refresh_token"),
        "token_type": token.get("token_type") or "Bearer",
        "scope": token.get("scope"),
        "expires_at": int(now + int(token["expires_in"])) if token.get("expires_in") else None,
    }
    if not rec["access_token"]:
        raise ValueError("token response has no access_token")
    _backend.set(_token_item(connector_id), json.dumps(rec))
    return rec


def load_token(connector_id: str) -> dict | None:
    raw = _backend.get(_token_item(connector_id))
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) and rec.get("access_token") else None


def delete_token(connector_id: str) -> None:
    _backend.delete(_token_item(connector_id))


def save_client_secret(connector_id: str, secret: str) -> None:
    _backend.set(_secret_item(connector_id), secret)


def load_client_secret(connector_id: str) -> str | None:
    return _backend.get(_secret_item(connector_id))


def delete_client_secret(connector_id: str) -> None:
    _backend.delete(_secret_item(connector_id))
