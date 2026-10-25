"""Work with a deadline, so a folder that stops answering cannot hang the app.

The vault is a folder on somebody else's disk. A sync daemon can get
stuck, a volume can be rebuilding, a backup can be holding a lock — and
then every read of that folder waits forever. The product reads the vault
every hour and whenever somebody asks for the map, and a request that
waits for a folder that never answers is a request that never ends.

The work happens in a thread, so the caller gets an answer either way:
the result, or "the vault did not answer in time". The thread cannot be
killed, but it finishes by itself when the folder answers again, and it
is a daemon, so it never keeps the app alive on its own.

This is not a theory: the vault of this Mac stopped answering for hours
on 2026-10-24, and the export of the map — a request that took four
seconds — never ended.
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger("conductloop.bounded")

# How long the product waits for the vault before it gives up and says so.
DEFAULT_VAULT_TIMEOUT = 240.0


class Timeout(RuntimeError):
    """The work did not finish in time."""


def vault_timeout(default: float = DEFAULT_VAULT_TIMEOUT) -> float:
    """The deadline for reading the vault, in seconds."""
    raw = os.environ.get("CONDUCTLOOP_VAULT_TIMEOUT", "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def run(fn: Callable[[], Any], seconds: float, *, what: str = "the work") -> Any:
    """Run ``fn`` and give up after ``seconds``.

    Raises ``Timeout`` when the work is still running, and re-raises
    whatever the work itself raised.
    """
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — the caller has to see it
            box["error"] = e

    thread = threading.Thread(target=runner, name="bounded-work", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        log.warning("%s did not answer in %ss: giving up on it", what, seconds)
        raise Timeout(f"{what} did not answer in {seconds:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")
