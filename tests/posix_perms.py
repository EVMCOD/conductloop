"""Assertions about POSIX permission bits, which Windows does not have.

``chmod(0o600)`` only toggles the read-only attribute on Windows and ``st_mode``
reports 0o666 for ordinary files, so a test that asserts "owner only" or "is
executable" cannot pass there, and it is not a bug (docs/PORTABILITY.md 4.3,
``ops_dashboard/fileperms.py``). Two ways to deal with such a test, and the only
two the guard in tests/test_windows_workflow.py accepts:

* the whole test is about the bits: mark it ``@pytest.mark.posix_perms`` (skipped
  on Windows by tests/conftest.py, with the reason below);
* the bits are one assertion among others: make that assertion with one of the
  helpers here, which do nothing on Windows, and keep the rest of the test
  running there.

macOS and Linux assert exactly what they asserted before.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

REASON = ("POSIX permission bits (st_mode, chmod, the executable bit) do not exist on Windows "
          "(docs/PORTABILITY.md 4.3)")


def on_windows() -> bool:
    return sys.platform == "win32"


def assert_mode(path: str | os.PathLike[str], expected: int) -> None:
    """``S_IMODE(stat(path)) == expected``, except on Windows."""
    if on_windows():
        return
    actual = stat.S_IMODE(Path(path).stat().st_mode)
    assert actual == expected, f"{path}: mode {oct(actual)}, expected {oct(expected)}"


def assert_no_group_other_access(path: str | os.PathLike[str]) -> None:
    """No permission bit for group or others, except on Windows."""
    if on_windows():
        return
    actual = stat.S_IMODE(Path(path).stat().st_mode)
    assert actual & 0o077 == 0, f"{path}: mode {oct(actual)} gives access to group/others"


def assert_executable(path: str | os.PathLike[str]) -> None:
    """The file has an execute bit, except on Windows."""
    if on_windows():
        return
    assert Path(path).stat().st_mode & 0o111, f"{path} is not executable"
