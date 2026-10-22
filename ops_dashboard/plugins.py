"""Plugin registry: custom indexers loaded from ~/.ops-dashboard/plugins/*.py.

Each plugin is a Python file with:
    NAME = "my-plugin"
    def run() -> dict:
        # do something, return a stats dict
        return {"indexed": 5}

Plugins are auto-discovered on startup. The 'ops index custom' command
runs all registered plugins. To create a plugin:
    1. mkdir -p ~/.ops-dashboard/plugins
    2. Write my_plugin.py with NAME and run()
    3. python -m ops_dashboard index custom
"""
from __future__ import annotations

import importlib.util
import sys

from . import app_paths as _app_paths

PLUGIN_DIR = _app_paths.path("plugins")

_discovered: dict = {}


def discover() -> dict:
    """Scan PLUGIN_DIR and load all .py files."""
    global _discovered
    _discovered = {}
    if not PLUGIN_DIR.exists():
        PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        example = PLUGIN_DIR / "_example.py"
        if not example.exists():
            example.write_text(
                '''# Example plugin — drop a .py file in this folder with NAME and run()
NAME = "example"
def run():
    # do your work, return a stats dict
    return {"indexed": 0, "note": "edit me!"}
''',
                encoding="utf-8",
            )
    for p in PLUGIN_DIR.glob("*.py"):
        if p.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"plugin_{p.stem}", p)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[f"plugin_{p.stem}"] = mod
            spec.loader.exec_module(mod)
            if hasattr(mod, "NAME") and hasattr(mod, "run"):
                _discovered[mod.NAME] = mod.run
        except Exception as e:
            print(f"plugin error: {p.name}: {e}")
    return _discovered


def run_all() -> dict:
    """Run all discovered plugins. Returns aggregate stats."""
    if not _discovered:
        discover()
    totals = {"indexed": 0, "plugins": 0, "errors": 0}
    for name, fn in _discovered.items():
        try:
            r = fn()
            if isinstance(r, dict):
                totals["indexed"] += r.get("indexed", 0)
                totals["plugins"] += 1
            print(f"  plugin {name}: {r}")
        except Exception as e:
            totals["errors"] += 1
            print(f"  plugin {name} error: {e}")
    return totals
