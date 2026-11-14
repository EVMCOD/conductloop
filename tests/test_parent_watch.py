"""The bundled server exits when the desktop app that started it is gone."""
from __future__ import annotations

import subprocess
import sys
import threading

from ops_dashboard import parent_watch


def test_calls_back_once_the_parent_exits():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
    gone = threading.Event()
    parent_watch.watch(child.pid, gone.set, interval=0.05)
    assert not gone.wait(0.1)                 # still running
    child.wait()
    assert gone.wait(2.0)


def test_alive():
    import os
    assert parent_watch.alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not parent_watch.alive(p.pid)
