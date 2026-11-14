"""app_paths: a fresh install never creates ~/ops-dashboard; an existing one keeps its files."""
from __future__ import annotations

from ops_dashboard import app_paths


def test_fresh_install_uses_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(app_paths, "LEGACY_HOME", tmp_path / "home" / "ops-dashboard")
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPS_CONFIG_PATH", raising=False)
    assert app_paths.config_path() == tmp_path / "data" / "config.json"
    assert app_paths.path("backups") == tmp_path / "data" / "backups"
    assert app_paths.path("models", "data/models") == tmp_path / "data" / "models"
    assert not (tmp_path / "home").exists()                      # nothing created in $HOME


def test_existing_legacy_files_keep_being_used(tmp_path, monkeypatch):
    legacy = tmp_path / "home" / "ops-dashboard"
    (legacy / "data" / "models").mkdir(parents=True)
    (legacy / "config.json").write_text("{}")
    monkeypatch.setattr(app_paths, "LEGACY_HOME", legacy)
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPS_CONFIG_PATH", raising=False)
    assert app_paths.config_path() == legacy / "config.json"
    assert app_paths.path("models", "data/models") == legacy / "data" / "models"
    assert app_paths.path("backups") == tmp_path / "data" / "backups"   # no legacy backups dir


def test_existing_legacy_library_is_the_data_dir(tmp_path, monkeypatch):
    legacy = tmp_path / "home" / "ops-dashboard"
    (legacy / "data").mkdir(parents=True)
    monkeypatch.setattr(app_paths, "LEGACY_HOME", legacy)
    monkeypatch.setattr(app_paths, "_REPO_DATA", tmp_path / "no-repo" / "data")
    monkeypatch.delenv("OPS_DATA_DIR", raising=False)
    assert app_paths.data_dir() != legacy / "data"            # an empty folder is not a library
    (legacy / "data" / "ops.db").write_bytes(b"")
    assert app_paths.data_dir() == legacy / "data"
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path / "data"))
    assert app_paths.data_dir() == tmp_path / "data"          # the override still wins


def test_fresh_install_without_legacy_uses_the_platform_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(app_paths, "LEGACY_HOME", tmp_path / "home" / "ops-dashboard")
    monkeypatch.setattr(app_paths, "_REPO_DATA", tmp_path / "no-repo" / "data")
    monkeypatch.delenv("OPS_DATA_DIR", raising=False)
    d = app_paths.data_dir()
    assert "ops-dashboard" not in str(d) and d.name == "data"
    assert not (tmp_path / "home").exists()


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_CONFIG_PATH", str(tmp_path / "x.json"))
    assert app_paths.config_path() == tmp_path / "x.json"


def test_health_reports_the_desktop_app_version(monkeypatch):
    from ops_dashboard import routes_meta
    monkeypatch.setenv("CONDUCTLOOP_APP_VERSION", "1.3.0")
    assert routes_meta._version() == "1.3.0"


def test_desktop_app_finds_the_project_registry(tmp_path, monkeypatch):
    import json

    from ops_dashboard import projects
    legacy = tmp_path / "home" / "ops-dashboard"
    legacy.mkdir(parents=True)
    (legacy / "projects.local.json").write_text(json.dumps([{"key": "alpha", "name": "Alpha"}]))
    monkeypatch.setattr(app_paths, "LEGACY_HOME", legacy)
    monkeypatch.delenv("OPS_PROJECTS_FILE", raising=False)
    monkeypatch.delenv("CONDUCTLOOP_DEMO", raising=False)
    # inside the app bundle there is no projects.local.json next to the code
    monkeypatch.setattr(projects, "__file__", str(tmp_path / "bundle" / "ops_dashboard" / "projects.py"))
    assert [p.key for p in projects._load_projects()] == ["alpha"]
