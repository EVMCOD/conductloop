"""Owner-only permissions for files that hold secrets, as far as the OS allows.

On macOS and Linux this is ``chmod 0600``. On Windows ``os.chmod`` only toggles
the read-only attribute: the mode bits for group and others do not exist, so
``0o600`` achieves nothing there. We do not pretend otherwise: on Windows this
does nothing and reports ``False`` (the file keeps the ACL it inherited from the
directory under ``%LOCALAPPDATA%``, which is limited to the user, SYSTEM and
Administrators). Real secrets belong in the vault or the OS credential store, not
in a file whose protection is a permission bit. It never raises: a filesystem that
refuses chmod (FAT, some network shares) must not break saving a file.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def restrict_to_owner(path: str | os.PathLike[str], mode: int = 0o600) -> bool:
    """Best-effort ``chmod(mode)`` (0600 for files; pass 0o700 for directories).

    Returns True when the mode was applied, False when it was skipped (Windows)
    or refused by the filesystem. Never raises."""
    if sys.platform == "win32":
        return False
    try:
        Path(path).chmod(mode)
    except OSError as e:
        log.debug("chmod %o failed for %s: %s", mode, path, type(e).__name__)
        return False
    return True
