"""The servers panel: what the product can talk to, and to whom (route
registry; imported by routes_meta).

GET  /api/servers        the configuration of every capability — no network
POST /api/servers/check  ask the servers themselves whether they are there
"""
from __future__ import annotations

import json

from . import servers
from .routing import router


def _send(handler, code: int, obj: dict) -> None:
    handler._send(code, json.dumps(obj).encode(), "application/json")


@router.get("/api/servers")
def servers_status(handler) -> None:
    """Every capability, the server that will serve it, and its state.

    Configuration only: this never waits for a network, so the page opens
    as fast as the app does.
    """
    try:
        _send(handler, 200, {"ok": True, **servers.status()})
    except Exception as e:
        _send(handler, 500, {"ok": False, "error": str(e)})


@router.post("/api/servers/check")
def servers_check(handler) -> None:
    """Ask the servers whether they are there.

    Slow by nature: a server that is not there takes seconds to say so. The
    answers are asked in parallel, so the wait is the slowest one.
    """
    try:
        answers = servers.probe()
    except Exception as e:
        _send(handler, 500, {"ok": False, "error": str(e)})
        return
    _send(handler, 200, {"ok": True, "answers": answers})
