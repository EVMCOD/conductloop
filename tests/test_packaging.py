"""`pip install .` must ship every runtime subpackage (sessions, learning,
skills, …) — a missing one only shows up as an ImportError in a real install."""
from __future__ import annotations

import tomllib
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_every_runtime_subpackage_is_packaged():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["packages"]["find"]
    on_disk = {".".join(p.parent.relative_to(ROOT).parts) for p in (ROOT / "ops_dashboard").rglob("__init__.py")
               if "__pycache__" not in p.parts}
    # setuptools' find: fnmatch on dotted names (no setuptools import needed in CI)
    packaged = {p for p in on_disk
                if any(fnmatchcase(p, pat) for pat in cfg["include"])
                and not any(fnmatchcase(p, pat) for pat in cfg.get("exclude", ()))}
    runtime = {p for p in on_disk if not p.startswith("ops_dashboard.evals")}
    assert runtime <= packaged, sorted(runtime - packaged)
    assert "ops_dashboard.sessions" in packaged
    assert not any(p.startswith("ops_dashboard.evals") for p in packaged)
