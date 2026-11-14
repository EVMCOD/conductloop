"""The marks of the agents: the identity of ConductLoop, drawn small.

The twelve marks are the ones designed in the identity kit. The script
``scripts/build_agent_marks.py`` cropped each one out of the board and wrote
the movement that goes with it next to them (``web/agent-marks.css``); this
module only serves the two.

Local only: the marks are part of the app, not something the network asks
for.
"""
from __future__ import annotations

import functools
import os
from collections.abc import Callable

from .routing import router

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MARKS_DIR = os.path.join(WEB_DIR, "assets", "agents")

# The names that exist. The route does not take the name from the request
# and read a file with it: a name that is not a mark of an agent is a 404.
MARKS = frozenset({
    "cso", "cfo", "cto", "cmo",
    "vp_design", "vp_frontend", "vp_mobile", "vp_devops",
    "head_product", "head_sales", "general_counsel", "head_cs",
})


def _local_only(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(handler, **kwargs) -> None:
        if not handler._is_local():
            handler._send(403, b"local only", "text/plain")
            return
        fn(handler, **kwargs)
    return wrapper


@router.get("/assets/agents/<name>.png")
@_local_only
def agent_mark(handler, name: str) -> None:
    """GET /assets/agents/<role>.png — the mark of an agent."""
    if name not in MARKS:
        handler._send(404, b"no such mark", "text/plain")
        return
    try:
        with open(os.path.join(MARKS_DIR, f"{name}.png"), "rb") as fh:
            body = fh.read()
    except OSError:
        handler._send(404, b"no such mark", "text/plain")
        return
    handler._send(200, body, "image/png",
                  headers={"Cache-Control": "public, max-age=86400"})


@router.get("/agent-marks.css")
@_local_only
def agent_marks_css(handler) -> None:
    """GET /agent-marks.css — the movement of the marks."""
    try:
        with open(os.path.join(WEB_DIR, "agent-marks.css"), "rb") as fh:
            body = fh.read()
    except OSError:
        handler._send(404, b"no such file", "text/plain")
        return
    handler._send(200, body, "text/css; charset=utf-8",
                  headers={"Cache-Control": "public, max-age=86400"})
