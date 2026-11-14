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

_SKIP = {"base"}

# The modules are named here on purpose. A frozen build does not ship the
# package as files: its modules live inside the PYZ archive, so walking the
# folder finds nothing there. These connectors used to register themselves by
# walking it, and in the frozen app the walk came back empty — no connector
# registered itself, the page showed zero connectors, and nothing raised.
#
# A test walks the folder and fails when this list and the folder drift
# apart, so a connector added later is not forgotten here.
_MODULES = (
    "github",
    "linear",
    "notion",
    "readwise",
    "rss",
    "todoist",
)


def load_all() -> list[str]:
    loaded = []
    for name in _MODULES:
        if name in _SKIP:
            continue
        importlib.import_module(f"{__name__}.{name}")
        loaded.append(name)
    return loaded
