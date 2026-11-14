"""Mach-O loadability of the Rust build artifacts (scripts/check_macho_dylibs.py).

macOS 27's dyld refuses to dlopen() a dylib whose LC_SYMTAB string table is not
8-byte aligned, and Xcode 27.2's ld64 produces such dylibs when the deployment
target enables chained fixups. rustc dlopen()s the proc-macro dylibs it builds,
so a misaligned one kills the whole build with "can't find crate for
`phf_macros`" — an error that has nothing to do with the crate that failed.

These tests pin the checker's behaviour and the wiring that keeps the build
producing loadable dylibs.
"""

from __future__ import annotations

import importlib.util
import os
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_macho_dylibs", ROOT / "scripts" / "check_macho_dylibs.py"
)
assert _spec and _spec.loader
cmd = importlib.util.module_from_spec(_spec)
# dataclasses needs the module registered before it is executed
sys.modules["check_macho_dylibs"] = cmd
_spec.loader.exec_module(cmd)

RELEASE_SCRIPT = ROOT / "scripts" / "release-desktop.sh"
CARGO_CONFIG = ROOT / "desktop" / "src-tauri" / ".cargo" / "config.toml"

_MACHO_64 = 0xFEEDFACF
_LC_SEGMENT_64 = 0x19
_LC_SYMTAB = 0x02
_LC_DYLD_CHAINED_FIXUPS = 0x80000033


def _macho_bytes(stroff: int, *, chained: bool = True, nsyms: int = 61) -> bytes:
    """Build a minimal 64-bit Mach-O whose string table sits at `stroff`."""
    segment = struct.pack(
        "<II16sQQQQiiII",
        _LC_SEGMENT_64,
        72,
        b"__LINKEDIT",
        0,
        0x1000,
        0x2000,
        0x1000,
        0,  # maxprot
        0,  # initprot
        0,  # nsects
        0,  # flags
    )
    symtab = struct.pack("<IIIIII", _LC_SYMTAB, 24, 0x2000, nsyms, stroff, 0x400)
    commands = [segment, symtab]
    if chained:
        commands.append(struct.pack("<IIIIII", _LC_DYLD_CHAINED_FIXUPS, 16, 0x2000, 16, 0, 0))
    body = b"".join(commands)
    header = struct.pack(
        "<IIIIIIII",
        _MACHO_64,
        0x0100000C,  # CPU_TYPE_ARM64
        0,
        6,  # MH_DYLIB
        len(commands),
        len(body),
        0,
        0,
    )
    return header + body


def _write_macho(path: Path, stroff: int, **kwargs) -> Path:
    path.write_bytes(_macho_bytes(stroff, **kwargs))
    return path


def test_read_macho_exposes_the_string_table(tmp_path: Path) -> None:
    path = _write_macho(tmp_path / "libfoo.dylib", 0x2A3DAC)
    macho = cmd.read_macho(path)
    assert macho.stroff == 0x2A3DAC
    assert macho.nsyms == 61
    assert macho.chained_fixups is True
    assert macho.fixup_style == "chained-fixups"


def test_aligned_string_table_has_no_problem(tmp_path: Path) -> None:
    path = _write_macho(tmp_path / "libfoo.dylib", 0x2A3DA8)
    assert cmd.read_macho(path).alignment == 0
    assert cmd.check_dylib(path, load=False) == []


def test_misaligned_string_table_is_reported(tmp_path: Path) -> None:
    path = _write_macho(tmp_path / "libfoo.dylib", 0x2A3DAC)
    problems = cmd.check_dylib(path, load=False)
    assert len(problems) == 1
    assert "mis-aligned" in problems[0] or "not 8" in problems[0]
    assert "libfoo.dylib" in problems[0]


def test_check_deps_dir_reports_the_bad_dylib(tmp_path: Path) -> None:
    _write_macho(tmp_path / "libgood.dylib", 0x2A3DA8)
    _write_macho(tmp_path / "libbad.dylib", 0x2A3DAC)
    problems = cmd.check_deps_dir(tmp_path, load=False)
    assert len(problems) == 1
    assert "libbad.dylib" in problems[0]
    assert "libgood.dylib" not in problems[0]


def test_check_binary_reports_a_misaligned_executable(tmp_path: Path) -> None:
    path = _write_macho(tmp_path / "conductloop-desktop", 0x2A3DAC)
    problems = cmd.check_binary(path)
    assert len(problems) == 1
    assert "conductloop-desktop" in problems[0]


def test_a_file_that_is_not_a_macho_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "libfake.dylib"
    path.write_bytes(b"not a macho" * 8)
    problems = cmd.check_dylib(path, load=False)
    assert len(problems) == 1
    assert "not a 64-bit Mach-O" in problems[0]


def test_a_real_system_dylib_loads() -> None:
    if sys.platform != "darwin":
        pytest.skip("dyld is macOS-only")
    ok, reason = cmd.dylib_loads(Path("/usr/lib/libSystem.B.dylib"))
    assert ok, reason


def test_release_script_runs_the_checker() -> None:
    script = RELEASE_SCRIPT.read_text()
    assert "check_macho_dylibs.py" in script
    assert "--deps-dir" in script


def test_check_deps_dir_skips_stale_dylibs(tmp_path: Path) -> None:
    """Cargo leaves the dylibs of older builds in the target dir: only the
    artifacts of the current build are this release's business."""
    stale = _write_macho(tmp_path / "libstale.dylib", 0x2A3DAC)
    fresh = _write_macho(tmp_path / "libfresh.dylib", 0x2A3DA8)
    os.utime(stale, (1_000_000, 1_000_000))
    os.utime(fresh, (2_000_000, 2_000_000))
    assert cmd.check_deps_dir(tmp_path, load=False) != []  # without a filter it sees both
    assert cmd.check_deps_dir(tmp_path, load=False, since=1_500_000) == []


def test_release_script_checks_only_the_current_build() -> None:
    script = RELEASE_SCRIPT.read_text()
    assert "--since" in script
    assert "BUILD_START" in script


def test_release_script_remaps_paths_at_compile_time() -> None:
    """The home path is rewritten by the compiler, not patched into the binary:
    a replacement of the same length only works for homes of a given size."""
    script = RELEASE_SCRIPT.read_text()
    rustflags = next(line for line in script.splitlines() if line.startswith("export RUSTFLAGS="))
    assert "--remap-path-prefix" in rustflags
    assert "REMAP_TARGET" not in script
    assert "the replacement path must be as long" not in script


def test_build_disables_chained_fixups() -> None:
    """The build must not use chained fixups: they are what misaligns the dylibs."""
    assert "-Wl,-no_fixup_chains" in RELEASE_SCRIPT.read_text()
    assert "-Wl,-no_fixup_chains" in CARGO_CONFIG.read_text()


def test_cargo_config_only_touches_apple_targets() -> None:
    """The flag is ld64-only: on other targets it would break the link."""
    config = CARGO_CONFIG.read_text()
    apple = [line for line in config.splitlines() if "no_fixup_chains" in line]
    assert apple, "the cargo config must carry the flag"
    assert "apple-darwin" in config
    for target in ("linux", "windows", "wasm"):
        assert f"{target}" not in config
