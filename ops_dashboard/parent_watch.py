"""Stop the bundled server when the desktop app that started it is gone.

The app only killed the sidecar when its window was destroyed, which Cmd+Q
does not always reach, and the kill hit PyInstaller's launcher, not the Python
process holding :7777. The orphan kept serving, so the next (updated) app talked
to the old server. The app now passes its PID; this watches it.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable

# Win32 constants for the liveness check (see ``_alive_windows``).
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_TIMEOUT = 0x00000102
_ERROR_INVALID_PARAMETER = 87    # OpenProcess: no process has this id


def _kernel32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _alive_windows(pid: int, k32=None, get_last_error=None) -> bool:
    """Liveness of ``pid`` on Windows, without signalling it.

    ``os.kill(pid, 0)`` must never be used there: CPython maps every signal other
    than CTRL_C_EVENT/CTRL_BREAK_EVENT to ``TerminateProcess``, so the "check"
    would kill the process it is checking (here: the desktop app). Instead open a
    handle with the least rights and wait on it for 0 ms: it is signalled once the
    process has exited.

    Only "no such process" counts as gone. Any other failure (access denied, an
    unexpected error) is reported as alive: a wrong "alive" leaves an orphan for a
    while, a wrong "gone" would stop a healthy server. UNTESTED on real Windows
    (the unit tests use a fake kernel32).
    """
    if pid <= 0:
        return False
    import ctypes
    k = k32 if k32 is not None else _kernel32()
    last_error = get_last_error or (lambda: ctypes.get_last_error())  # type: ignore[attr-defined]
    handle = k.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return last_error() != _ERROR_INVALID_PARAMETER
    try:
        return k.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        k.CloseHandle(handle)


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:      # exists, owned by someone else
        return True
    return True


def watch(pid: int, on_gone: Callable[[], None], *, interval: float = 2.0) -> threading.Thread:
    """Call ``on_gone()`` once, from a daemon thread, when ``pid`` exits."""
    def loop() -> None:
        while alive(pid):
            time.sleep(interval)
        on_gone()

    t = threading.Thread(target=loop, name="parent-watch", daemon=True)
    t.start()
    return t
