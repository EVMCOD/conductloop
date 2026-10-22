"""Stop the bundled server when the desktop app that started it is gone.

The app only killed the sidecar when its window was destroyed, which Cmd+Q
does not always reach, and the kill hit PyInstaller's launcher, not the Python
process holding :7777. The orphan kept serving, so the next (updated) app talked
to the old server. The app now passes its PID; this watches it.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable


def alive(pid: int) -> bool:
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
