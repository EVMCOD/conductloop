#!/usr/bin/env python3
"""Fail fast when a Mach-O file from the Rust build cannot be loaded by dyld.

macOS 27's dyld refuses to ``dlopen()`` a dylib whose ``LC_SYMTAB`` string
table is not 8-byte aligned ("mis-aligned LINKEDIT string pool"). Xcode 27.2's
ld64 (``ld-27106``) lays out ``__LINKEDIT`` that way when the deployment
target enables chained fixups, and whether the string table lands on 8 or on 4
bytes then depends on the size of the symbol table — so some crates come out
aligned and some do not.

The damage is invisible until the build dies with an error that has nothing to
do with the crate that failed::

    error[E0463]: can't find crate for `phf_macros`

rustc ``dlopen()``s the proc-macro dylibs it just built, so one misaligned
artifact kills the whole build — and, because the corrupt file keeps its name,
every later build keeps failing on it.

This script checks the artifacts right after the build:

* every dylib in the cargo target dir: the string table offset is 8-byte
  aligned **and** dyld actually loads it;
* the app binary: the string table offset is 8-byte aligned. dyld is more
  lenient with the main executable, but a misaligned one is a latent risk.

Usage::

    python3 scripts/check_macho_dylibs.py \\
        --deps-dir desktop/src-tauri/target/release/deps \\
        --binary desktop/src-tauri/target/release/bundle/macos/ConductLoop.app/Contents/MacOS/conductloop-desktop

Stdlib only, so it runs on a bare macOS python3 and in CI.
"""

from __future__ import annotations

import argparse
import ctypes
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

# dyld wants the __LINKEDIT string table 8-byte aligned.
STRING_TABLE_ALIGNMENT = 8

_MACHO_64 = 0xFEEDFACF
_LC_SEGMENT_64 = 0x19
_LC_SYMTAB = 0x02
_LC_DYLD_CHAINED_FIXUPS = 0x80000033


class MachOError(ValueError):
    """Raised when a file is not a 64-bit Mach-O we can read."""


@dataclass(frozen=True)
class MachO:
    """The few facts about a Mach-O this check cares about."""

    path: Path
    symoff: int
    nsyms: int
    stroff: int
    strsize: int
    linkedit_fileoff: int
    linkedit_filesize: int
    chained_fixups: bool

    @property
    def alignment(self) -> int:
        """How the string table is aligned (0 means aligned)."""
        return self.stroff % STRING_TABLE_ALIGNMENT

    @property
    def fixup_style(self) -> str:
        return "chained-fixups" if self.chained_fixups else "classic-dyld-info"


def read_macho(path: Path) -> MachO:
    """Read the load commands of a 64-bit Mach-O file."""
    data = path.read_bytes()
    if len(data) < 32:
        raise MachOError(f"{path}: too short to be a Mach-O ({len(data)} bytes)")
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic != _MACHO_64:
        raise MachOError(f"{path}: not a 64-bit Mach-O (magic {magic:#010x})")

    ncmds = struct.unpack_from("<I", data, 16)[0]
    offset = 32
    symtab: tuple[int, int, int, int] | None = None
    linkedit: tuple[int, int] | None = None
    chained = False
    for _ in range(ncmds):
        if offset + 8 > len(data):
            raise MachOError(f"{path}: truncated load commands")
        cmd, cmdsize = struct.unpack_from("<II", data, offset)
        if cmdsize < 8:
            raise MachOError(f"{path}: bad load command size ({cmdsize})")
        if cmd == _LC_SYMTAB:
            symtab = struct.unpack_from("<IIII", data, offset + 8)
        elif cmd == _LC_SEGMENT_64:
            segname = data[offset + 8 : offset + 24].rstrip(b"\0")
            fileoff, filesize = struct.unpack_from("<QQ", data, offset + 40)
            if segname == b"__LINKEDIT":
                linkedit = (fileoff, filesize)
        elif cmd == _LC_DYLD_CHAINED_FIXUPS:
            chained = True
        offset += cmdsize

    if symtab is None:
        raise MachOError(f"{path}: no LC_SYMTAB")
    if linkedit is None:
        raise MachOError(f"{path}: no __LINKEDIT segment")
    symoff, nsyms, stroff, strsize = symtab
    return MachO(
        path=path,
        symoff=symoff,
        nsyms=nsyms,
        stroff=stroff,
        strsize=strsize,
        linkedit_fileoff=linkedit[0],
        linkedit_filesize=linkedit[1],
        chained_fixups=chained,
    )


def dylib_loads(path: Path) -> tuple[bool, str]:
    """Ask dyld to load the dylib. Returns (ok, reason)."""
    try:
        ctypes.CDLL(str(path))
    except OSError as exc:
        return False, str(exc)
    return True, ""


def check_dylib(path: Path, *, load: bool = True) -> list[str]:
    """Return the problems of a single dylib (empty list means it is fine)."""
    problems: list[str] = []
    try:
        macho = read_macho(path)
    except MachOError as exc:
        return [str(exc)]

    if macho.alignment:
        problems.append(
            f"{path.name}: the __LINKEDIT string table is at offset {macho.stroff:#x}, "
            f"which is {macho.alignment}-byte aligned and not {STRING_TABLE_ALIGNMENT}. "
            f"dyld refuses to load such a dylib ('mis-aligned LINKEDIT string pool')."
        )
    if load:
        ok, reason = dylib_loads(path)
        if not ok:
            problems.append(f"{path.name}: dyld cannot load it: {reason}")
    return problems


def check_deps_dir(deps_dir: Path, *, load: bool = True, since: float | None = None) -> list[str]:
    """Check every dylib in a cargo target dir.

    ``since`` (epoch seconds) restricts the check to the artifacts of the
    current build. Cargo keeps the dylibs of older builds in the target dir
    (different flags give different hashes in the file name), and those are not
    what this build shipped: rejecting them would fail a release whose current
    build is fine.
    """
    problems: list[str] = []
    if not deps_dir.is_dir():
        return [f"{deps_dir}: not a directory"]
    for dylib in sorted(deps_dir.glob("*.dylib")):
        if since is not None and dylib.stat().st_mtime < since - 2:
            continue
        problems.extend(check_dylib(dylib, load=load))
    return problems


def check_binary(path: Path) -> list[str]:
    """Check the app binary. dyld is lenient here, so a misalignment is a warning."""
    try:
        macho = read_macho(path)
    except MachOError as exc:
        return [str(exc)]
    if macho.alignment:
        return [
            f"{path.name}: the __LINKEDIT string table is at offset {macho.stroff:#x}, "
            f"{macho.alignment}-byte aligned and not {STRING_TABLE_ALIGNMENT}."
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deps-dir", type=Path, help="cargo target dir holding the proc-macro dylibs")
    parser.add_argument("--binary", type=Path, help="the app binary to check")
    parser.add_argument(
        "--no-load",
        action="store_true",
        help="only check the alignment, do not ask dyld to load the dylibs",
    )
    parser.add_argument(
        "--since",
        type=float,
        help="epoch seconds: only check the dylibs built after this moment",
    )
    args = parser.parse_args(argv)

    load = not args.no_load
    problems: list[str] = []
    checked = 0

    if args.deps_dir:
        problems.extend(check_deps_dir(args.deps_dir, load=load, since=args.since))
        checked = len(
            [
                d
                for d in args.deps_dir.glob("*.dylib")
                if args.since is None or d.stat().st_mtime >= args.since - 2
            ]
        ) if args.deps_dir.is_dir() else 0

    if args.binary:
        problems.extend(check_binary(args.binary))
        checked += 1

    if not args.deps_dir and not args.binary:
        parser.error("pass --deps-dir and/or --binary")

    if problems:
        print("✗ the build produced Mach-O files dyld cannot use:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\n  On macOS 27 + Xcode 27.2 this is the chained-fixups layout: the string\n"
            "  table ends up 4-byte aligned and dyld rejects the dylib. Build with\n"
            "  `-C link-arg=-Wl,-no_fixup_chains` (see desktop/src-tauri/.cargo/config.toml).",
            file=sys.stderr,
        )
        return 1

    print(f"  ✓ {checked} Mach-O file(s) are aligned and loadable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
