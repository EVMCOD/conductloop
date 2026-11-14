"""Chat channels: talk to your agents from a chat app, and get the answers back.

Telegram and Discord are the chat channels (``telegram``, ``discord``). Every
channel is off until the user turns it on, its token lives in the vault (never
in the database, the configuration or a log), and it only talks to chats the
user paired from the app with a one-time code. See ``docs/CHANNELS.md``.
"""
from __future__ import annotations

import os
import threading

_lock = threading.Lock()
_runners: dict[str, object] = {}


def disabled_by_env() -> bool:
    """``OPS_DISABLE_CHANNELS=1`` keeps every channel stopped (kill switch)."""
    return os.environ.get("OPS_DISABLE_CHANNELS") == "1" or os.environ.get("CONDUCTLOOP_DEMO") == "1"


def start() -> None:
    """Start the channel workers. They idle (no network) while a channel is off."""
    if disabled_by_env():
        return
    from . import discord, telegram
    with _lock:
        for name, mod in (("telegram", telegram), ("discord", discord)):
            runner = _runners.get(name)
            if runner is None:
                runner = mod.Poller()
                _runners[name] = runner
            runner.start()  # type: ignore[attr-defined]


def stop() -> None:
    with _lock:
        for runner in _runners.values():
            try:
                runner.stop()  # type: ignore[attr-defined]
            except Exception:
                pass
