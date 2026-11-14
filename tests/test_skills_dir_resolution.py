"""The skills directory must never resolve to the process CWD.

``OPS_SKILLS_DIR`` set to the empty string is "present but empty".
``Path(os.environ.get("X", default))`` returns ``Path("")`` -> ``.`` in that
case, so the app would read and write skills inside whatever directory it
happened to be launched from. Treat empty as unset.
"""
from __future__ import annotations

from pathlib import Path

from ops_dashboard.skills import catalog


def test_empty_env_var_does_not_resolve_to_the_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("OPS_SKILLS_DIR", "")
    resolved = catalog.vault_dir()
    assert resolved != Path(".")
    assert resolved.is_absolute()


def test_unset_env_var_uses_the_vault_default(monkeypatch):
    monkeypatch.delenv("OPS_SKILLS_DIR", raising=False)
    resolved = catalog.vault_dir()
    assert resolved.is_absolute()
    assert resolved.name == "skills"


def test_a_real_value_is_still_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("OPS_SKILLS_DIR", str(tmp_path / "custom"))
    assert catalog.vault_dir() == tmp_path / "custom"


def test_a_whitespace_only_value_does_not_resolve_to_the_cwd(monkeypatch):
    monkeypatch.setenv("OPS_SKILLS_DIR", "   ")
    resolved = catalog.vault_dir()
    assert resolved != Path(".")
    assert resolved.is_absolute()
