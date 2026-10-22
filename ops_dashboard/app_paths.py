"""Where ConductLoop keeps its files.

Everything lives in the app's data directory (``data_dir()``, the folder of
``ops.db``). Older builds and dev setups used fixed paths under
``~/ops-dashboard/`` (the original repo location); if such a file or folder
already exists it keeps being used, so nothing is lost, but nothing new is
ever created there: a fresh install never makes a ``~/ops-dashboard`` folder
in the user's home.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

LEGACY_HOME = Path.home() / "ops-dashboard"


def data_dir() -> Path:
    """The data directory (holds ops.db). Three-tier lookup:
      1. $OPS_DATA_DIR                  — explicit override
      2. <repo>/data                    — dev checkout, if that folder exists
      3. macOS ~/Library/Application Support/com.conductloop.desktop/data
         Linux ~/.local/share/conductloop/data · Windows %LOCALAPPDATA%/ConductLoop/data
    """
    env = os.environ.get("OPS_DATA_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent.parent / "data"
    if here.exists() and here.is_dir():
        return here
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "com.conductloop.desktop" / "data"
    if sys.platform.startswith("linux"):
        return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "conductloop" / "data"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ConductLoop" / "data"
    return here


def path(rel: str, legacy_rel: str | None = None) -> Path:
    """``data_dir()/rel``, unless ``~/ops-dashboard/<legacy_rel or rel>``
    already exists (an older install or a dev setup keeps its files)."""
    legacy = LEGACY_HOME / (legacy_rel or rel)
    if legacy.exists():
        return legacy
    return data_dir() / rel


def config_path() -> Path:
    """config.json (provider keys, defaults). $OPS_CONFIG_PATH wins."""
    env = os.environ.get("OPS_CONFIG_PATH")
    return Path(env) if env else path("config.json")
