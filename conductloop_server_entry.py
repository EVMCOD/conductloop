"""ConductLoop server entrypoint for PyInstaller.

Used by both `python -m ops_dashboard` (dev) and the Tauri sidecar
(launched as a single executable from <bundle>/Contents/MacOS/conductloop-server).

The wrapper used to live here as a separate shell script; instead the env
defaults are baked into this entry so Tauri can spawn this binary directly.
"""
import os
import sys
from pathlib import Path


def _set_data_dir_if_missing() -> None:
    """Pick a persistent, writable location for ops.db if the caller didn't
    override OPS_DATA_DIR.  Used by both the .app and `python -m` dev paths."""
    if os.environ.get("OPS_DATA_DIR"):
        return
    sys_plat = sys.platform
    try:
        home = Path.home()
    except Exception:
        return  # home may not be set in some Tauri contexts; skip
    if sys_plat == "darwin":
        d = home / "Library" / "Application Support" / "com.conductloop.desktop" / "data"
    elif sys_plat.startswith("linux"):
        d = Path(os.environ.get("XDG_DATA_HOME", str(home / ".local" / "share"))) / "conductloop" / "data"
    elif sys_plat.startswith("win"):
        d = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))) / "ConductLoop" / "data"
    else:
        # last-resort: alongside the bundle (read-only when shipped, only writable in dev)
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return  # read-only mount (DMG); skip and let brain.py/db.py do their thing
    os.environ["OPS_DATA_DIR"] = str(d)
    # Re-set for the per-user config too; Linux/Windows users get a default
    cfg = d.parent
    os.environ.setdefault("OPS_CONFIG_PATH", str(cfg / "config.json"))


_set_data_dir_if_missing()


# No CLI args at all → default to serving the dashboard (Tauri sidecar
# spawn path).
if len(sys.argv) == 1:
    sys.argv = ["conductloop-server", "serve"]


from ops_dashboard.cli import app


if __name__ == "__main__":
    sys.exit(app())
