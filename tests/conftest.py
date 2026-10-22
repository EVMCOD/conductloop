"""Keep the test suite away from the developer's real data.

ops_dashboard picks its data directory when it is imported (``db.DB_PATH``).
In a dev checkout that is ``<repo>/data``: the same ops.db that ``make serve``
and the desktop app use. Tests that did not isolate themselves wrote demo rows,
test users and duplicate team members into it, and anything resolved through
``app_paths`` could reach ``~/ops-dashboard`` (config.json with API keys,
backups, plugins...). Point every default at a throwaway directory before any
test module imports ops_dashboard. Tests that need their own paths still set
them, as before.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_SANDBOX = Path(tempfile.mkdtemp(prefix="conductloop-tests-"))
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

os.environ["OPS_DATA_DIR"] = str(_SANDBOX / "data")
os.environ["OPS_CONFIG_PATH"] = str(_SANDBOX / "config.json")
# An empty registry, so a local projects.local.json in the checkout is not read.
(_SANDBOX / "projects.local.json").write_text("[]")
os.environ["OPS_PROJECTS_FILE"] = str(_SANDBOX / "projects.local.json")
os.environ.pop("CONDUCTLOOP_DB_PATH", None)

from ops_dashboard import app_paths  # noqa: E402  (must run after the env above)

app_paths.LEGACY_HOME = _SANDBOX / "legacy-home"
