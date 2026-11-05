"""Automatable pieces of the Mac release checklist (issue #32).

The remaining items need Enrique's Keychain, OAuth consoles, a packaged
WKWebView, or a live L2 key — those stay on the issue as a manual list.
These tests run in CI and on `scripts/mac-checks.sh` without touching the
real Application Support database or any secrets.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from ops_dashboard import migrations
from ops_dashboard.sessions import sandbox

# Versions the Mac checklist explicitly asks to cross-check against migrations.py.
CHECKLIST_MIG_RANGE = range(20, 25)  # 20..24 inclusive


def test_schema_migration_descriptions_match_migrations_py(tmp_path, monkeypatch):
    """A DB that once ran a different branch with the same version numbers
    would silently skip a migration. Descriptions in schema_migrations must
    match the source of truth in migrations.MIGRATIONS."""
    expected = {v: d for v, d, _ in migrations.MIGRATIONS if v in CHECKLIST_MIG_RANGE}
    assert set(expected) == set(CHECKLIST_MIG_RANGE), (
        f"migrations.py no longer defines every version in {list(CHECKLIST_MIG_RANGE)}; "
        f"got {sorted(expected)}"
    )

    dbp = tmp_path / "desc.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()

    with db.get_connection() as conn:
        rows = {
            int(v): d
            for v, d in conn.execute(
                "SELECT version, description FROM schema_migrations "
                "WHERE version BETWEEN 20 AND 24"
            )
        }
    assert rows == expected


def test_migration_20_rejects_an_unexpected_agent_sessions_schema(tmp_path):
    """If agent_sessions was created by another branch, rebuild must refuse
    rather than rewrite SQL it does not recognise (would brick start-up)."""
    dbp = tmp_path / "odd.db"
    conn = sqlite3.connect(dbp)
    conn.execute(
        "CREATE TABLE session_repos ("
        "repo_path TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
    )
    # Deliberately missing the exact NOT NULL clauses migration 20 swaps on.
    conn.execute(
        "CREATE TABLE agent_sessions ("
        "id INTEGER PRIMARY KEY,"
        "name TEXT NOT NULL,"
        "role TEXT NOT NULL,"
        "provider TEXT NOT NULL,"
        "model TEXT NOT NULL,"
        "repo_path TEXT REFERENCES session_repos(repo_path),"
        "base_ref TEXT,"
        "budget_cost_cents INTEGER NOT NULL,"
        "created_at REAL NOT NULL,"
        "updated_at REAL NOT NULL)"
    )
    conn.commit()
    with pytest.raises(RuntimeError, match="migration 20: unexpected agent_sessions schema"):
        migrations._mig_20_browser_sessions(conn)
    conn.close()


def test_migration_20_through_24_on_a_wal_safe_backup_copy(tmp_path, monkeypatch):
    """Issue #32: work on a copy via sqlite3 .backup, never cp of a live WAL DB.

    Uses a synthetic DB only — never the real Application Support path.
    """
    from ops_dashboard import db
    from ops_dashboard.sessions import SessionStore

    src = tmp_path / "src.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(src))
    monkeypatch.setattr(db, "DB_PATH", src)
    # Stop at v8 so migration 20 still has work to do on the copy.
    full = list(migrations.MIGRATIONS)
    monkeypatch.setattr(migrations, "MIGRATIONS", [m for m in full if m[0] <= 8])
    db.init_db()
    st = SessionStore()
    st.mark_repo(str(tmp_path))
    sid = st.create_session(
        name="c", role="c", provider="openai", model="m",
        repo_path=str(tmp_path), base_ref="main", budget_cost_cents=500,
    )
    before_count = 1

    copy = tmp_path / "copy.db"
    a, b = sqlite3.connect(src), sqlite3.connect(copy)
    a.backup(b)
    a.close()
    b.close()

    monkeypatch.setattr(migrations, "MIGRATIONS", full)
    conn = sqlite3.connect(copy)
    conn.execute("PRAGMA foreign_keys = ON")
    applied = migrations.run_migrations(conn)
    conn.commit()
    applied_versions = {v for v, _ in applied}
    assert set(CHECKLIST_MIG_RANGE) <= applied_versions

    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0] == before_count
    cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_sessions)")]
    assert "browser_mode" in cols
    assert conn.execute(
        "SELECT id FROM agent_sessions WHERE id = ?", (sid,)
    ).fetchone()[0] == sid

    expected = {v: d for v, d, _ in full if v in CHECKLIST_MIG_RANGE}
    rows = {
        int(v): d
        for v, d in conn.execute(
            "SELECT version, description FROM schema_migrations "
            "WHERE version BETWEEN 20 AND 24"
        )
    }
    assert rows == expected

    assert migrations.run_migrations(conn) == []  # second run applies nothing
    conn.close()


def test_seatbelt_profile_denies_network_and_sensitive_paths(tmp_path):
    prof = sandbox.seatbelt_profile(worktree=tmp_path, tmp=tmp_path)
    assert "(deny network*)" in prof
    assert "(deny file-write*" in prof
    # Absolute, OS-native paths (backslashes on Windows).
    ssh = str(Path.home() / ".ssh")
    keychains = str(Path.home() / "Library" / "Keychains")
    assert ssh in prof
    assert keychains in prof


@pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
    reason="sandbox-exec only exists on macOS",
)
def test_detect_backend_is_sandbox_exec_on_macos(monkeypatch):
    monkeypatch.delenv("CONDUCTLOOP_SANDBOX", raising=False)
    sandbox._PROBED.clear()
    assert sandbox.detect_backend() == "sandbox-exec"


@pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
    reason="sandbox-exec only exists on macOS",
)
def test_broken_seatbelt_profile_fails_closed(tmp_path, monkeypatch):
    """If the profile cannot load, the command must not run (issue #32)."""
    marker = tmp_path / "should-not-exist"
    monkeypatch.setattr(
        sandbox, "seatbelt_profile",
        lambda **_k: "(this is not a valid seatbelt profile",
    )
    sandbox._PROBED.clear()
    result = sandbox.run(
        [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"],
        worktree=tmp_path,
        cwd=tmp_path,
        path_dirs=[str(Path(sys.executable).parent)],
        backend="sandbox-exec",
        timeout_s=20,
    )
    assert not marker.exists(), "broken profile must not let the command run"
    assert result.exit_code not in (0, None)


@pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
    reason="sandbox-exec only exists on macOS",
)
def test_sandbox_exec_blocks_network_write_and_ssh_read(tmp_path):
    """The three probes from the Mac checklist, against a real sandbox-exec."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "written.txt"
    wt = tmp_path / "wt"
    wt.mkdir()
    ssh = Path.home() / ".ssh"
    probe = f"""
import os, socket, sys
errs = []
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3)
    errs.append("network")
except OSError:
    pass
try:
    open({str(target)!r}, "w").write("x")
    errs.append("write")
except OSError:
    pass
ssh = {str(ssh)!r}
if os.path.isdir(ssh):
    try:
        names = os.listdir(ssh)
        # empty dir is fine; a readable non-empty listing is the failure
        if names:
            errs.append("ssh")
    except OSError:
        pass
print("OK" if not errs else "FAIL:" + ",".join(errs))
sys.exit(0 if not errs else 1)
"""
    (wt / "probe.py").write_text(probe, encoding="utf-8")
    result = sandbox.run(
        [sys.executable, "probe.py"],
        worktree=wt,
        cwd=wt,
        path_dirs=[str(Path(sys.executable).parent)],
        backend="sandbox-exec",
        timeout_s=30,
    )
    assert result.sandbox == "sandbox-exec"
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert not target.exists()


def test_mac_checks_script_exists_and_mentions_port_and_manual_items():
    script = Path(__file__).resolve().parent.parent / "scripts" / "mac-checks.sh"
    text = script.read_text(encoding="utf-8")
    assert script.exists()
    assert "7777" in text
    assert "Keychain" in text or "keychain" in text
    assert "issue #32" in text or "issue 32" in text or "#32" in text
