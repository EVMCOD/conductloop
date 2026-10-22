"""Connectors that need no OAuth: a personal token or no auth at all
(GitHub, Linear, Todoist, RSS, Readwise…). The OAuth connectors (Google,
Slack, Notion, Microsoft…) live in ``connectors_native``; both packages share
the vault convention (``documents.source = <connector id>``, ``path =
<connector id>/<kind>/<key>``) and both run their ``sync`` action through the
#40 framework.

Each connector lives in its own module in this package and registers itself
on import with ``base.register_connector``. ``load_all()`` imports every module
here, so adding a connector never touches a shared file (registry, routes,
ci.yml or the mypy ratchet).
"""
from __future__ import annotations

import importlib
import pkgutil

_SKIP = {"base"}


def load_all() -> list[str]:
    loaded = []
    for m in pkgutil.iter_modules(__path__):
        if m.name.startswith("_") or m.name in _SKIP:
            continue
        importlib.import_module(f"{__name__}.{m.name}")
        loaded.append(m.name)
    return loaded
