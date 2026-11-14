#!/usr/bin/env python3
"""Fail when the Python environment that freezes the app does not satisfy
the requirements it is supposed to ship.

The desktop sidecar is frozen by PyInstaller from whatever is installed in the
repo's .venv. Nothing checked that .venv against requirements.txt, so it drifted:
1.2.22 shipped fastembed 0.9.0 (the file asks for >=0.9.1) and with it a
Pillow that has known vulnerabilities, although requirements.txt already
allowed the fixed one. A floor in a requirements file protects nobody if the
build never installs it.

Usage: python scripts/check_build_env.py [requirements.txt ...]
Exit 0 when every requirement that applies to this platform is installed at a
version the file allows; 1 otherwise, listing each problem.
"""
from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement


def iter_requirements(path: Path, seen: set[Path] | None = None):
    """Yield (file, Requirement) for every line, following -r includes."""
    seen = seen if seen is not None else set()
    path = path.resolve()
    if path in seen:
        return
    seen.add(path)
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split(" #")[0].split("\t#")[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement ")):
            yield from iter_requirements(path.parent / line.split(None, 1)[1], seen)
            continue
        if line.startswith("-"):
            continue  # other pip options (index URLs, -e …) are not version floors
        try:
            yield path.name, Requirement(line)
        except InvalidRequirement as exc:
            raise SystemExit(f"{path.name}: cannot parse {line!r}: {exc}") from None


def problems(paths: list[Path]) -> list[str]:
    out: list[str] = []
    for fname, req in (r for p in paths for r in iter_requirements(p)):
        if req.marker is not None and not req.marker.evaluate():
            continue
        try:
            installed = version(req.name)
        except PackageNotFoundError:
            out.append(f"{fname}: {req.name} is not installed (wants {req.specifier or 'any'})")
            continue
        if req.specifier and not req.specifier.contains(installed, prereleases=True):
            out.append(f"{fname}: {req.name} {installed} does not satisfy {req.specifier}")
    return out


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] or [Path("requirements.txt")]
    found = problems(paths)
    if found:
        print("The build environment does not match the requirements:", file=sys.stderr)
        for p in found:
            print(f"  - {p}", file=sys.stderr)
        print("Fix: .venv/bin/pip install -r " + " -r ".join(str(p) for p in paths), file=sys.stderr)
        return 1
    print("build environment satisfies " + ", ".join(str(p) for p in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
