"""Coverage for the users / auth / sessions / device-tokens /
usage surface of db.py — the security-sensitive code the smoke sweep can't
reach (localhost bypasses auth). This is the guard for splitting that section
out into its own module.

Needs bcrypt (password hashing); skipped where it isn't installed.
    .venv/bin/pytest tests/test_auth.py -v
"""
from __future__ import annotations

import pytest

pytest.importorskip("bcrypt")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "auth.db"))
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "auth.db"  # type: ignore[attr-defined]
    _db.init_db()
    return _db


def test_password_hash_and_verify(db):
    u = db.create_user(email="A@Example.com", password="s3cret", name="Al")
    assert u and u["email"] == "a@example.com"  # normalized lower-case
    assert u["password_hash"] and u["password_hash"] != "s3cret"  # bcrypt-hashed, not plaintext
    assert db.verify_password(u, "s3cret") is True
    assert db.verify_password(u, "wrong") is False
    # duplicate email -> None (unique constraint)
    assert db.create_user(email="a@example.com", password="x") is None
    assert db.get_user_by_email("a@example.com")["id"] == u["id"]


def test_user_without_password_cannot_verify(db):
    u = db.create_user(email="np@example.com")
    assert db.verify_password(u, "anything") is False


def test_update_user(db):
    u = db.create_user(email="up@example.com", password="p")
    assert db.update_user(u["id"], name="Renamed", locale="es") is True
    got = db.get_user(u["id"])
    assert got["name"] == "Renamed" and got["locale"] == "es"


def test_session_lifecycle(db):
    u = db.create_user(email="s@example.com", password="p")
    tok = db.create_session(u["id"])
    assert db.get_session(tok)["user_id"] == u["id"]
    assert db.delete_session(tok) is True
    assert db.get_session(tok) is None


def test_expired_session_not_returned_and_purged(db):
    u = db.create_user(email="e@example.com", password="p")
    tok = db.create_session(u["id"], days=-1)  # already expired
    assert db.get_session(tok) is None
    assert db.purge_expired_sessions() >= 1


def test_device_token_stores_hash_not_raw(db):
    raw = db.create_device_token(name="phone")
    assert raw and len(raw) > 20
    row = db.get_device_token(raw)
    assert row and row["name"] == "phone"
    assert row["token_hash"] != raw  # only the hash is persisted
    assert db.get_device_token("bogus") is None
    db.touch_device_token(raw)
    assert len(db.list_device_tokens()) == 1
    assert db.revoke_device_token(row["id"]) is True
    assert db.get_device_token(raw) is None  # revoked -> unresolvable


def test_usage_metering(db):
    db.log_usage(provider="anthropic", model="opus", prompt_tokens=10, completion_tokens=5)
    db.log_usage(provider="anthropic", prompt_tokens=3, completion_tokens=2)
    s = db.usage_summary(days=1)
    assert s["calls"] == 2
    assert s["total_tokens"] == 20
    assert s["by_provider"][0]["provider"] == "anthropic"
