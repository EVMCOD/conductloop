"""The URL checks keep a history, so "it just went down" can be answered.

The current-state table keeps one row per URL — the name is unique and every
check replaces the previous one — so it can say whether a URL is up right now
and nothing else. The history table appends one row per check, which is what
makes a flip readable.

Before this, `status_changes()` compared the current row with a "previous" row
that could not exist: with one row per URL the window function always found
`rn = 2` empty, the delta was always "new", and the list of changes was always
empty. The endpoint `/api/live-urls/changes` said a URL never changed, for
ever.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "b.db"))
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    from ops_dashboard import brain, db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "b.db")
    monkeypatch.setattr(brain, "CONFIG_PATH", tmp_path / "config.json")
    db.init_db()
    return db, tmp_path


def _check(env, name: str, ok: bool) -> None:
    """One check of one URL, the way the indexer records it."""
    from ops_dashboard.indexers import live_urls

    live_urls.ensure_schema()
    live_urls._record(
        {"name": name, "url": f"https://{name}.test", "project": "acme", "kind": "site"},
        {
            "status_code": 200 if ok else 500,
            "ok": 1 if ok else 0,
            "latency_ms": 42,
            "error": None if ok else "HTTPError: 500",
            "final_url": f"https://{name}.test",
        },
    )


def _delta(env, name: str) -> str:
    from ops_dashboard.indexers import live_urls

    for row in live_urls.status_changes():
        if row["name"] == name:
            return row["delta"]
    raise AssertionError(f"{name} is not in the changes")


def test_a_url_that_goes_down_is_reported_as_failed(env):
    _check(env, "site", ok=True)
    _check(env, "site", ok=False)
    assert _delta(env, "site") == "failed"


def test_a_url_that_comes_back_is_reported_as_recovered(env):
    _check(env, "site", ok=True)
    _check(env, "site", ok=False)
    _check(env, "site", ok=True)
    assert _delta(env, "site") == "recovered"


def test_a_url_that_stays_up_is_unchanged(env):
    _check(env, "site", ok=True)
    _check(env, "site", ok=True)
    assert _delta(env, "site") == "unchanged"


def test_the_first_check_of_a_url_is_new(env):
    _check(env, "site", ok=True)
    assert _delta(env, "site") == "new"


def test_the_history_keeps_every_check_and_the_state_keeps_one_row(env):
    """The current state is the last thing known; the history is everything."""
    from ops_dashboard import db

    for ok in (True, False, True, True):
        _check(env, "site", ok=ok)

    with db.db() as conn:
        state = conn.execute("SELECT COUNT(*) FROM live_urls WHERE name='site'").fetchone()[0]
        history = conn.execute("SELECT COUNT(*) FROM live_url_checks WHERE name='site'").fetchone()[0]
    assert state == 1
    assert history == 4


def test_a_url_configured_but_never_checked_is_not_a_change(env):
    """Configuring a URL is not checking it."""
    from ops_dashboard.indexers import live_urls

    live_urls.upsert_url("site", "https://site.test", project="acme")
    assert live_urls.status_changes() == []


def test_a_url_that_has_a_history_is_not_reported_twice(env):
    from ops_dashboard.indexers import live_urls

    _check(env, "site", ok=True)
    _check(env, "site", ok=False)
    names = [r["name"] for r in live_urls.status_changes()]
    assert names == ["site"]


def test_changes_since_returns_the_checks_that_changed(env):
    """'What changed since last time I looked?' means the flips, not every check."""
    from ops_dashboard.indexers import live_urls

    _check(env, "site", ok=True)
    _check(env, "site", ok=True)   # unchanged
    _check(env, "site", ok=False)  # changed
    _check(env, "site", ok=True)   # changed

    import time

    changes = live_urls.changes_since(int(time.time()) - 3600)
    assert [(c["name"], bool(c["ok"])) for c in changes] == [("site", True), ("site", False)]


def test_a_url_with_no_history_is_still_reported_as_new(env):
    """An install that was checked before this table existed keeps working:
    its URLs are new until they are checked again."""
    from ops_dashboard import db
    from ops_dashboard.indexers import live_urls

    live_urls.ensure_schema()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO live_urls (name, url, checked_at, status_code, ok) VALUES ('old','https://old.test',?,200,1)",
            (1,),
        )
    assert _delta(env, "old") == "new"
