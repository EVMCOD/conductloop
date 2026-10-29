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

import pytest

_SANDBOX = Path(tempfile.mkdtemp(prefix="conductloop-tests-"))
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

os.environ["OPS_DATA_DIR"] = str(_SANDBOX / "data")
os.environ["OPS_CONFIG_PATH"] = str(_SANDBOX / "config.json")
# An empty registry, so a local projects.local.json in the checkout is not read.
(_SANDBOX / "projects.local.json").write_text("[]")
os.environ["OPS_PROJECTS_FILE"] = str(_SANDBOX / "projects.local.json")
os.environ.pop("CONDUCTLOOP_DB_PATH", None)
# No test writes to the keychain of the machine that runs it. A key typed
# into a test would end up in the login keychain of whoever is developing,
# and the keychain of a CI runner is nobody's business. The tests that
# need a keychain patch one in (or turn this off on purpose).
os.environ["OPS_DISABLE_KEYCHAIN"] = "1"

from ops_dashboard import app_paths  # noqa: E402  (must run after the env above)

app_paths.LEGACY_HOME = _SANDBOX / "legacy-home"

# The passphrase every test opens the vault with. There is no keychain in a
# test (see above), so the vault is the encrypted file, and a test that
# stores a key needs it open. The passphrase is a constant of the test
# suite and lands in a throwaway directory.
TEST_PASSPHRASE = "conductloop-tests"


@pytest.fixture(autouse=True)
def _vault_open():
    """Open the vault for every test, and close it afterwards."""
    from ops_dashboard import secret_vault

    secret_vault.lock()
    secret_vault.unlock(TEST_PASSPHRASE)
    yield
    secret_vault.lock()


def pytest_collection_modifyitems(config, items):
    """On Windows, skip what cannot exist there, each with its reason.

    * the files and tests of tests/windows_skips.py;
    * every test marked ``posix_perms`` (asserts POSIX permission bits).
    """
    import sys

    if sys.platform != "win32":
        return
    from posix_perms import REASON as PERMS_REASON
    from windows_skips import SKIPPED_ON_WINDOWS

    for item in items:
        name = Path(str(item.fspath)).name
        test = getattr(item, "originalname", None) or item.name
        reason = SKIPPED_ON_WINDOWS.get(name) or SKIPPED_ON_WINDOWS.get(f"{name}::{test}")
        if reason:
            item.add_marker(pytest.mark.skip(reason=f"not on Windows: {reason}"))
        elif item.get_closest_marker("posix_perms"):
            item.add_marker(pytest.mark.skip(reason=f"not on Windows: {PERMS_REASON}"))
