"""BYOK key-storage routes (route registry; imported by routes_meta).

GET  /api/brain/keys/status   banner state (no key values, ever)
POST /api/brain/keys/migrate  {"consent": true[, "providers": [...]]}  LOCAL ONLY
POST /api/brain/keys/revert   {"consent": true[, "providers": [...]]}  LOCAL ONLY
POST /api/brain/keys/dismiss  user said "not now" to the migration prompt
"""
from __future__ import annotations

import json
import threading

from . import brain, byok_keys
from .routing import router

_cfg_lock = threading.Lock()


def _send(handler, code: int, obj: dict) -> None:
    handler._send(code, json.dumps(obj).encode(), "application/json")


def _local_only(handler) -> bool:
    if not handler._is_local():
        _send(handler, 403, {"ok": False, "error": "local only"})
        return False
    return True


def _providers(data: dict) -> list[str] | None:
    raw = data.get("providers")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise ValueError("providers must be a list of provider names")
    return raw


@router.get("/api/brain/keys/status")
def keys_status(handler) -> None:
    _send(handler, 200, byok_keys.status(brain._load_config()))


def _consented_op(handler, op) -> None:
    if not _local_only(handler):
        return
    data = handler._read_json()
    if data.get("consent") is not True:
        _send(handler, 400, {"ok": False, "error": "consent required"})
        return
    try:
        providers = _providers(data)
        with _cfg_lock:
            cfg = brain._load_config()
            result = op(cfg, consent=True, providers=providers)
            if result.get("moved") or result.get("restored"):
                brain._save_config(cfg)
            state = byok_keys.status(cfg)
    except ValueError as e:
        _send(handler, 400, {"ok": False, "error": str(e)})
        return
    _send(handler, 200, {**result, "status": state})


@router.post("/api/brain/keys/migrate")
def keys_migrate(handler) -> None:
    _consented_op(handler, byok_keys.migrate_to_keychain)


@router.post("/api/brain/keys/revert")
def keys_revert(handler) -> None:
    _consented_op(handler, byok_keys.revert_to_config)


@router.post("/api/brain/keys/dismiss")
def keys_dismiss(handler) -> None:
    if not _local_only(handler):
        return
    with _cfg_lock:
        cfg = brain._load_config()
        byok_keys.dismiss_prompt(cfg)
        brain._save_config(cfg)
    _send(handler, 200, {"ok": True, "status": byok_keys.status(cfg)})
