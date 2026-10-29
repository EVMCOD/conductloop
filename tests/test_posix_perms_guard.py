"""A test that looks at POSIX permission bits must say so (so Windows can skip it).

Windows has no such bits: ``chmod(0o600)`` is a no-op there and ``st_mode`` reads
0o666. A test that asserts them fails on ``windows-latest`` for a reason that is
not a bug. Rather than discovering each one in a red run, this test reads every
test file and fails when it finds one that inspects permission bits (``st_mode``,
``S_IMODE``/``S_I*`` constants, ``os.access`` with an R/W/X flag) outside the
accepted ways (see tests/posix_perms.py):

* the test function carries ``@pytest.mark.posix_perms`` (or the module has
  ``pytestmark = pytest.mark.posix_perms``);
* the check is done through the helpers of tests/posix_perms.py;
* the whole file, or that test, is in tests/windows_skips.py with a reason.

``chmod`` used to *set up* a fixture (a fake executable) is not an inspection and
is not flagged.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from windows_skips import SKIPPED_ON_WINDOWS  # noqa: E402

# The helper module and this guard talk about the bits without asserting them.
EXEMPT_FILES = {"posix_perms.py", "test_posix_perms_guard.py"}
ACCESS_FLAGS = {"R_OK", "W_OK", "X_OK"}
STAT_CONSTANT = re.compile(r"^S_(IMODE|IFMT|ISUID|ISGID|ISVTX|I[RWX](USR|GRP|OTH|EXEC)|IRWX[UGO]|IREAD|IWRITE)$")


def _names(node: ast.AST):
    if isinstance(node, ast.Attribute):
        yield node.attr
    elif isinstance(node, ast.Name):
        yield node.id


def _found(root: ast.AST):
    """(line, what) for each permission-bit inspection under ``root``."""
    for n in ast.walk(root):
        for name in _names(n):
            if name == "st_mode" or STAT_CONSTANT.match(name):
                yield n.lineno, name
        if isinstance(n, ast.Call):
            f = n.func
            if getattr(f, "attr", getattr(f, "id", None)) == "access" and any(
                    flag in ACCESS_FLAGS for a in n.args for flag in _names(a)):
                yield n.lineno, "os.access(..., R_OK/W_OK/X_OK)"


def inspections(tree: ast.Module):
    """Yield (owning test function or None, line, what). Methods of a class own themselves."""
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        if isinstance(node, funcs):
            owned = [(node, node)]
        elif isinstance(node, ast.ClassDef):
            owned = [(m, m) if isinstance(m, funcs) else (m, None) for m in node.body]
        else:
            owned = [(node, None)]
        for root, owner in owned:
            for line, what in _found(root):
                yield owner, line, what


def _marked(fn) -> bool:
    return fn is not None and any("posix_perms" in ast.unparse(d) for d in fn.decorator_list)


def _module_marked(tree: ast.Module) -> bool:
    return any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets)
               and "posix_perms" in ast.unparse(n.value) for n in tree.body)


def problems(path: Path, source: str | None = None) -> list[str]:
    name = path.name
    if name in EXEMPT_FILES or name in SKIPPED_ON_WINDOWS:
        return []
    tree = ast.parse(source if source is not None else path.read_text(encoding="utf-8"))
    if _module_marked(tree):
        return []
    out = []
    for fn, line, what in inspections(tree):
        if _marked(fn) or (fn is not None and f"{name}::{fn.name}" in SKIPPED_ON_WINDOWS):
            continue
        where = f"{fn.name}()" if fn is not None else "module level"
        out.append(f"{name}:{line}: {what} in {where}")
    return sorted(set(out))


def test_no_test_inspects_permission_bits_without_saying_so():
    files = sorted(TESTS.glob("test_*.py"))
    assert files
    found = [p for f in files for p in problems(f)]
    assert not found, (
        "These tests look at POSIX permission bits, which Windows does not have. Mark the test "
        "@pytest.mark.posix_perms, or assert through tests/posix_perms.py (assert_mode, "
        "assert_no_group_other_access, assert_executable):\n  " + "\n  ".join(found))


# ── the detector itself: it must not pass on nothing ─────────────────────────

BAD = '''
import os, stat
def test_a(tmp_path):
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
'''


def test_the_detector_flags_each_way_of_reading_the_bits():
    for snippet, what in [
        ("def test_x(p):\n    assert p.stat().st_mode & 0o111\n", "st_mode"),
        ("import stat\ndef test_x(p):\n    assert stat.S_IMODE(1)\n", "S_IMODE"),
        ("from stat import S_IXUSR\ndef test_x(p):\n    assert p & S_IXUSR\n", "S_IXUSR"),
        ("import os\ndef test_x(p):\n    assert os.access(p, os.X_OK)\n", "os.access"),
        ("import os\nclass TestX:\n    def test_x(self, p):\n        assert os.stat(p).st_mode\n", "st_mode"),
        ("import os\nassert os.stat('.').st_mode\n", "st_mode"),
    ]:
        got = problems(Path("test_fake.py"), snippet)
        assert got and what in got[0], (snippet, got)


def test_the_accepted_ways_are_accepted():
    marked = BAD.replace("def test_a", "@pytest.mark.posix_perms\ndef test_a")
    assert problems(Path("test_fake.py"), marked) == []
    assert problems(Path("test_fake.py"), "pytestmark = pytest.mark.posix_perms\n" + BAD) == []
    helper = "from posix_perms import assert_mode\ndef test_a(p):\n    assert_mode(p, 0o600)\n"
    assert problems(Path("test_fake.py"), helper) == []
    setup_only = "def test_a(tmp_path):\n    f = tmp_path / 'x'\n    f.write_text('')\n    f.chmod(0o755)\n"
    assert problems(Path("test_fake.py"), setup_only) == []
    assert problems(Path("test_fake.py"), BAD)  # unmarked: flagged
    # A comment or a string that merely mentions st_mode is not an inspection.
    assert problems(Path("test_fake.py"), 'def test_a():\n    # st_mode\n    assert "st_mode"\n') == []


def test_the_helpers_follow_the_real_os_not_a_monkeypatched_sys_platform(tmp_path, monkeypatch):
    """windows-latest run 40926549010: a test patched sys.platform to "linux" and the helper
    then asserted 0o600 on a real Windows file. The OS is decided once, at import."""
    import sys

    import posix_perms

    f = tmp_path / "f"
    f.write_text("x")
    f.chmod(0o644)
    monkeypatch.setattr(sys, "platform", "win32" if sys.platform != "win32" else "linux")
    if posix_perms.on_windows():
        posix_perms.assert_mode(f, 0o600)          # no-op on the real Windows
    else:
        with pytest.raises(AssertionError):
            posix_perms.assert_mode(f, 0o600)
