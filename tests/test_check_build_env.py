"""scripts/check_build_env.py: the release refuses a .venv below requirements.txt."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_build_env", Path(__file__).resolve().parents[1] / "scripts" / "check_build_env.py")
check_build_env = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_build_env)


@pytest.fixture
def installed(monkeypatch):
    versions = {"pillow": "12.3.0", "fastembed": "0.9.1", "httpx": "0.28.1"}

    def fake_version(name):
        try:
            return versions[name.lower()]
        except KeyError:
            raise check_build_env.PackageNotFoundError(name) from None

    monkeypatch.setattr(check_build_env, "version", fake_version)
    return versions


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_a_package_below_its_floor_is_reported(tmp_path, installed):
    req = _write(tmp_path, "requirements.txt", "pillow>=13.0.0\nfastembed>=0.9.1\n")
    found = check_build_env.problems([req])
    assert found == ["requirements.txt: pillow 12.3.0 does not satisfy >=13.0.0"]
    assert check_build_env.main([str(req)]) == 1


def test_includes_comments_and_markers_are_followed(tmp_path, installed):
    _write(tmp_path, "requirements-core.txt", "httpx>=0.28.1          # http client\n")
    req = _write(tmp_path, "requirements.txt", (
        "# full runtime\n"
        "-r requirements-core.txt\n"
        "fastembed>=0.9.1\n"
        'keyring>=25.0.0; sys_platform == "never-a-platform"   # skipped by its marker\n'
    ))
    assert check_build_env.problems([req]) == []
    assert check_build_env.main([str(req)]) == 0


def test_a_missing_package_is_reported(tmp_path, installed):
    req = _write(tmp_path, "requirements.txt", "onnxruntime>=1.31.1\n")
    assert check_build_env.problems([req]) == [
        "requirements.txt: onnxruntime is not installed (wants >=1.31.1)"]


def test_the_release_script_runs_the_check_before_freezing():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "release-desktop.sh").read_text()
    check = script.index("scripts/check_build_env.py requirements.txt")
    freeze = script.index(".venv/bin/pyinstaller desktop/conductloop_server.spec")
    assert check < freeze


def test_the_repo_requirements_carry_the_security_floors():
    root = Path(__file__).resolve().parents[1]
    reqs = {r.name.lower(): r for _f, r in check_build_env.iter_requirements(root / "requirements.txt")}
    assert reqs["pillow"].specifier.contains("13.0.0") and not reqs["pillow"].specifier.contains("12.3.0")
    assert not reqs["opentelemetry-api"].specifier.contains("1.45.1")
