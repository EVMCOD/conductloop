"""The connectors have to register in a frozen build too.

PyInstaller does not ship the package as files: its modules live inside the
PYZ archive, so ``pkgutil.iter_modules(__path__)`` finds nothing there. The
connectors registered themselves by walking that list, so in the frozen app
the walk came back empty, no connector registered itself, and the page
showed **zero connectors** — silently, because nothing raised.

The frozen app is what the user runs. A feature that only exists in the
development checkout is a feature that does not exist.

So the modules are named explicitly, and a test walks the package to make
sure the list and the folder never drift apart.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

from ops_dashboard import connector_registry as registry

PKGS = ("ops_dashboard.connectors_native", "ops_dashboard.connectors_token")


def _registered() -> set[str]:
    return {s.id for s in registry.all_specs() if s.auth != "mcp"}


def test_the_connectors_register_without_walking_the_folder(monkeypatch):
    """The frozen build has no .py files to walk.

    Patching the walk to return nothing reproduces exactly what the app
    sees, and the connectors have to register anyway.
    """
    monkeypatch.setattr(pkgutil, "iter_modules", lambda path: [])
    for name in list(sys.modules):
        if name.startswith("ops_dashboard.connectors_"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, "ops_dashboard.connectors_native", raising=False)
    monkeypatch.delitem(sys.modules, "ops_dashboard.connectors_token", raising=False)

    for pkg in PKGS:
        importlib.import_module(pkg)

    assert _registered(), (
        "no connector registered when the package cannot be walked: in the "
        "frozen app this is exactly what happens")


def test_the_module_list_matches_the_folder():
    """A connector added later must not be forgotten.

    The list is what the frozen build uses, so a new connector that is not
    on it would ship as a working feature in development and as nothing at
    all in the app.
    """
    root = Path(__file__).resolve().parent.parent
    for pkg in PKGS:
        mod = importlib.import_module(pkg)
        folder = root / Path(pkg.replace(".", "/"))
        on_disk = {p.stem for p in folder.glob("*.py")
                   if not p.stem.startswith("_") and p.stem not in ("__init__", "base")}
        listed = set(getattr(mod, "_MODULES", ()))
        assert listed == on_disk, (
            f"{pkg}: listed {sorted(listed)} but the folder has {sorted(on_disk)}. "
            f"A connector that is not on the list works in development and is "
            f"missing in the app.")


def test_every_connector_is_reachable_by_name():
    """Importable by name, which is what the frozen build does."""
    for pkg in PKGS:
        mod = importlib.import_module(pkg)
        for name in getattr(mod, "_MODULES", ()):
            importlib.import_module(f"{pkg}.{name}")
