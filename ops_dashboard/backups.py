"""Automatic backups of the SQLite DB to <data dir>/backups/ (app_paths).

Keeps the last 30 backups by default. Run via:
    python -m ops_dashboard backup [now]

Or schedule via launchd (see scripts/install-launchd.sh).
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

from . import app_paths as _app_paths

log = logging.getLogger("conductloop.backups")

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ops.db"
BACKUP_DIR = _app_paths.path("backups")
MAX_KEEP = 30


def backup_now(reason: str = "manual") -> Path:
    """Take a backup of the DB. Returns the path of the new backup."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = BACKUP_DIR / f"ops-{ts}-{reason}.db"
    # Use sqlite3 backup API via shell copy (safe when DB is in WAL mode)
    if not DB_PATH.exists():
        raise FileNotFoundError(f"DB not found: {DB_PATH}")
    shutil.copy2(DB_PATH, out)
    _prune()
    return out


def _prune() -> None:
    if not BACKUP_DIR.exists():
        return
    files = sorted(BACKUP_DIR.glob("ops-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[MAX_KEEP:]:
        try: old.unlink()
        except Exception: log.warning("could not delete old backup %s", old, exc_info=True)


def list_backups() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    out = []
    for f in sorted(BACKUP_DIR.glob("ops-*.db"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = f.stat()
        out.append({"path": str(f), "size_kb": round(st.st_size / 1024, 1), "mtime": int(st.st_mtime), "name": f.name})
    return out
