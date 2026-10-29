"""Guards the setup checklist: 9 well-formed checks, consistent totals, and
never raising even when things are unconfigured."""
from __future__ import annotations

import pytest


@pytest.fixture()
def onboarding(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("OPS_DISABLE_KEYCHAIN", "1")
    monkeypatch.setenv("OPS_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("OPS_LOG_DIR", str(tmp_path / "logs"))
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "s.db"  # type: ignore[attr-defined]
    _db.init_db()
    from ops_dashboard import onboarding
    return onboarding


def test_checklist_shape(onboarding):
    r = onboarding.checklist()
    assert r["total"] == 9
    assert len(r["checks"]) == 9
    assert 0 <= r["passed"] <= 9
    assert r["ready"] is (r["required_passed"] == r["required"])
    assert r["required"] == 6 and {c["id"] for c in r["checks"] if c["optional"]} == {"embeddings", "projects", "obsidian_sync"}
    steps = [c["step"] for c in r["checks"]]
    assert steps == list(range(1, 10))
    assert "edition" not in {c["id"] for c in r["checks"]}
    for c in r["checks"]:
        assert set(c) >= {"step", "id", "label", "ok", "detail"}
        assert isinstance(c["ok"], bool)


def test_passing_checks_reflect_environment(onboarding):
    r = onboarding.checklist()
    by_id = {c["id"]: c for c in r["checks"]}
    # DB was just initialized + migrated, and secrets/logging dirs are writable.
    assert by_id["database"]["ok"] is True
    assert by_id["secrets"]["ok"] is True
    assert by_id["logging"]["ok"] is True
    # A fresh install has no indexed documents yet.
    assert by_id["documents"]["ok"] is False


def test_details_need_no_terminal_in_the_desktop_app(onboarding, monkeypatch):
    import builtins
    import sys
    real_import = builtins.__import__

    def no_st(name, *a, **k):
        if name == "sentence_transformers":
            raise ImportError(name)
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_st)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr("ops_dashboard.projects.PROJECTS", [])
    by_id = {c["id"]: c for c in onboarding.checklist()["checks"]}
    for cid in ("embeddings", "projects", "obsidian_sync"):
        assert by_id[cid]["optional"] is True
        detail = by_id[cid]["detail"]
        assert "pip install" not in detail and "OPS_PROJECTS_FILE" not in detail and "`ops " not in detail, detail
    assert "not in the desktop app" in by_id["embeddings"]["detail"]
