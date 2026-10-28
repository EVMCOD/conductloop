"""Backups of the database, from Setup.

GET  /api/backups           the backups, newest first, and where they are
POST /api/backups           take one now
POST /api/backups/restore   put one back: {"name": "<file name>"}

The specific route is registered before the generic one: the router takes
the first match.
"""
from __future__ import annotations

import json

from . import backups
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


def _listing() -> dict:
    return {"dir": str(backups.backup_dir()), "keep": backups.MAX_KEEP,
            "backups": [{k: b[k] for k in ("name", "size_kb", "mtime", "reason")}
                        for b in backups.list_backups()]}


@router.get("/api/backups")
def backups_list(handler) -> None:
    try:
        _send(handler, 200, {"ok": True, **_listing()})
    except Exception as e:
        _err(handler, 500, "list_failed", str(e))


@router.post("/api/backups/restore")
def backups_restore(handler) -> None:
    name = str(_body(handler).get("name") or "")
    try:
        result = backups.restore(name)
    except backups.BackupError as e:
        _err(handler, 400, "cannot_restore", str(e))
        return
    except Exception as e:
        _err(handler, 500, "restore_failed", str(e))
        return
    _send(handler, 200, {"ok": True, **result, **_listing()})


@router.post("/api/backups")
def backups_create(handler) -> None:
    try:
        path = backups.backup_now("manual")
    except (backups.BackupError, FileNotFoundError) as e:
        _err(handler, 400, "cannot_back_up", str(e))
        return
    except Exception as e:
        _err(handler, 500, "backup_failed", str(e))
        return
    _send(handler, 200, {"ok": True, "backup": path.name, **_listing()})
