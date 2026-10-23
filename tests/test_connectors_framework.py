"""Connectors framework: registry, OAuth loopback + PKCE, keychain-only tokens,
read/write permissions, confirmation of every external write, local audit.

No network: every HTTP call goes through ``connectors_fake.FakeProvider``
(httpx.MockTransport with recorded responses) which raises on unknown hosts;
the only sockets are loopback (the OAuth receiver test).
"""
from __future__ import annotations

import json
import socket
import sqlite3
import time
import urllib.error
import urllib.request

import pytest
from connectors_fake import API_HOST, AUTH_HOST, FakeProvider, NetworkForbidden, fake_spec

from ops_dashboard import connector_actions as actions
from ops_dashboard import connector_oauth as oauth
from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens
from ops_dashboard import migrations


@pytest.fixture(autouse=True)
def _no_external_network():
    real = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise NetworkForbidden(f"network access in tests: {address!r}")
        return real(self, address)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", guarded)
        yield


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conn.db")
    db.init_db()
    return db


@pytest.fixture
def kc():
    mem = tokens.MemoryBackend()
    prev = tokens.use_backend(mem)
    try:
        yield mem
    finally:
        tokens.use_backend(prev)


@pytest.fixture
def provider():
    p = FakeProvider()
    prev = oauth.set_transport(p.transport())
    try:
        yield p
    finally:
        oauth.set_transport(prev)


@pytest.fixture
def spec():
    s = registry.register(fake_spec())
    try:
        yield s
    finally:
        registry.unregister(s.id)


def _connect(spec, provider, permission="read"):
    store.upsert_account(spec.id, client_id="my-client-id")
    flow = oauth.start(spec.id, permission, listen=False)
    cb = provider.authorize(flow["authorize_url"])
    return oauth.complete(cb["state"], cb.get("code"), cb.get("error"))


def _audit_text(db) -> str:
    with db.db() as conn:
        return json.dumps([list(r) for r in conn.execute("SELECT * FROM connector_audit").fetchall()])


# ── migration 14 ─────────────────────────────────────────────────────────────

def test_migration_14_tables_and_idempotent():
    conn = sqlite3.connect(":memory:")
    migrations.run_migrations(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"connector_accounts", "connector_actions", "connector_audit"} <= names
    assert migrations.current_version(conn) >= 14
    migrations._mig_14_connectors(conn)          # gap-fill safe: re-running is a no-op
    versions = [v for v, _d, _f in migrations.MIGRATIONS]
    assert len(versions) == len(set(versions)) and 14 in versions


def test_gap_fill_applies_14_on_db_already_past_it():
    """A DB that reached a higher version on another branch still gets 14."""
    conn = sqlite3.connect(":memory:")
    migrations.run_migrations(conn)
    conn.execute("DROP TABLE connector_audit")
    conn.execute("DELETE FROM schema_migrations WHERE version = 14")
    conn.execute("PRAGMA user_version = 20")
    applied = migrations.run_migrations(conn)
    assert (14, migrations.MIGRATIONS[[v for v, *_ in migrations.MIGRATIONS].index(14)][1]) in applied
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='connector_audit'").fetchone()
    # The intent is "the database ends up at the latest schema", not a
    # particular number: a hardcoded version breaks on every new migration.
    assert migrations.current_version(conn) == max(v for v, *_ in migrations.MIGRATIONS)


# ── registry ────────────────────────────────────────────────────────────────

def test_registry_validation_and_minimal_scopes():
    s = fake_spec("valid")
    assert s.scopes_for("read") == ["fake.read"]
    assert s.scopes_for("write") == ["fake.read", "fake.write"]
    with pytest.raises(registry.RegistryError):
        s.scopes_for("admin")
    bad = [
        fake_spec("Bad Id"),
        registry.ConnectorSpec(id="plainhttp", name="x", auth="oauth_pkce", allowed_hosts=(AUTH_HOST,),
                               authorize_url=f"http://{AUTH_HOST}/a", token_url=f"https://{AUTH_HOST}/t"),
        registry.ConnectorSpec(id="otherhost", name="x", auth="oauth_pkce", allowed_hosts=(AUTH_HOST,),
                               authorize_url=f"https://{AUTH_HOST}/a", token_url="https://elsewhere.test/t"),
        registry.ConnectorSpec(id="nohosts", name="x", auth="token", allowed_hosts=()),
        registry.ConnectorSpec(id="writenoscope", name="x", auth="oauth_pkce", allowed_hosts=(AUTH_HOST,),
                               authorize_url=f"https://{AUTH_HOST}/a", token_url=f"https://{AUTH_HOST}/t",
                               read_scopes=("r",),
                               actions={"w": registry.ActionSpec("w", "write", "w", lambda c, p: {})}),
    ]
    for b in bad:
        with pytest.raises(registry.RegistryError):
            registry.register(b)
    assert registry.get("plainhttp") is None


def test_pkce_pair_is_s256():
    import base64
    import hashlib
    v, c = oauth.pkce_pair()
    assert 43 <= len(v) <= 128
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert oauth.pkce_pair()[0] != v


# ── OAuth flow ──────────────────────────────────────────────────────────────

def test_start_requires_byok_client_id_and_keychain(tmpdb, kc, spec):
    with pytest.raises(oauth.OAuthError) as e:
        oauth.start(spec.id, "read", listen=False)
    assert e.value.code == "client_id_required"
    store.upsert_account(spec.id, client_id="cid")

    class _NoKeychain(tokens.MemoryBackend):
        def available(self):
            return False
    prev = tokens.use_backend(_NoKeychain())
    try:
        with pytest.raises(oauth.OAuthError) as e2:
            oauth.start(spec.id, "read", listen=False)
        assert e2.value.code == "keychain_unavailable"
    finally:
        tokens.use_backend(prev)


def test_authorize_url_read_only_scopes_and_pkce(tmpdb, kc, spec):
    from urllib.parse import parse_qs, urlparse
    store.upsert_account(spec.id, client_id="cid")
    f = oauth.start(spec.id, "read", listen=False)
    q = {k: v[0] for k, v in parse_qs(urlparse(f["authorize_url"]).query).items()}
    assert q["scope"] == "fake.read"                       # no write scope unless asked
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["state"] == f["state"] and q["client_id"] == "cid"
    assert urlparse(q["redirect_uri"]).hostname == "127.0.0.1"
    assert "code_verifier" not in f["authorize_url"]


def test_full_flow_tokens_only_in_keychain(tmpdb, kc, spec, provider):
    res = _connect(spec, provider, "read")
    assert res["status"] == "connected"
    acc = store.get_account(spec.id)
    assert acc["status"] == "connected" and acc["permission"] == "read"
    assert acc["granted_scopes"] == ["fake.read"] and acc["account_label"] == "ana@example.test"
    rec = tokens.load_token(spec.id)
    assert rec["access_token"] == "fake-access-READ-0001"
    assert set(kc.items) == {"connector-token:fakechat"}
    raw = tmpdb.DB_PATH.read_bytes()
    for wal in tmpdb.DB_PATH.parent.glob("conn.db*"):
        raw += wal.read_bytes()
    assert b"fake-access" not in raw and b"fake-refresh" not in raw
    assert "fake-access" not in _audit_text(tmpdb) and "verifier" not in _audit_text(tmpdb)
    events = [e["event"] for e in store.list_audit()]
    assert events[:2] == ["connected", "connect_started"]


def test_state_is_single_use_and_unknown_state_refused(tmpdb, kc, spec, provider):
    store.upsert_account(spec.id, client_id="cid")
    f = oauth.start(spec.id, "read", listen=False)
    cb = provider.authorize(f["authorize_url"])
    assert oauth.complete(cb["state"], cb["code"])["status"] == "connected"
    for st in (cb["state"], "forged-state"):
        with pytest.raises(oauth.OAuthError) as e:
            oauth.complete(st, cb["code"])
        assert e.value.code == "unknown_state"


def test_denied_expired_and_bad_verifier(tmpdb, kc, spec, provider):
    store.upsert_account(spec.id, client_id="cid")
    f = oauth.start(spec.id, "read", listen=False)
    cb = provider.authorize(f["authorize_url"], deny=True)
    assert oauth.complete(cb["state"], None, cb["error"])["error"] == "access_denied"

    f2 = oauth.start(spec.id, "read", listen=False, now=time.time() - oauth.FLOW_TTL - 5)
    cb2 = provider.authorize(f2["authorize_url"])
    assert oauth.complete(cb2["state"], cb2["code"])["status"] == "expired"

    f3 = oauth.start(spec.id, "read", listen=False)
    cb3 = provider.authorize(f3["authorize_url"])
    oauth._FLOWS[cb3["state"]].verifier = "x" * 64          # attacker without the verifier
    r3 = oauth.complete(cb3["state"], cb3["code"])
    assert r3["status"] == "error" and r3["error"] == "token_endpoint"
    assert store.get_account(spec.id)["status"] == "disconnected"
    assert tokens.load_token(spec.id) is None
    assert [e["event"] for e in store.list_audit()].count("connect_failed") == 3


def test_write_downgraded_when_user_unticks_write_scope(tmpdb, kc, spec):
    p = FakeProvider(grant_write=False)
    prev = oauth.set_transport(p.transport())
    try:
        res = _connect(spec, p, "write")
    finally:
        oauth.set_transport(prev)
    assert res["status"] == "connected" and res["permission"] == "read"
    assert store.get_account(spec.id)["permission"] == "read"


def test_refresh_on_expiry_and_failure_marks_error(tmpdb, kc, spec, provider):
    _connect(spec, provider, "read")
    later = time.time() + 3600
    assert oauth.access_token(spec.id, now=later) == "fake-access-REFRESHED-0003"
    assert tokens.load_token(spec.id)["refresh_token"] == "fake-refresh-0001"   # kept when not rotated
    provider.refresh_ok = False
    with pytest.raises(oauth.OAuthError) as e:
        oauth.access_token(spec.id, now=later + 7200)
    assert e.value.code == "refresh_failed"
    assert store.get_account(spec.id)["status"] == "error"


def test_client_secret_provider_uses_basic_auth_and_keychain(tmpdb, kc):
    s = registry.register(fake_spec("fakenotion", client_secret_required=True))
    p = FakeProvider(client_secret="byok-secret")
    prev = oauth.set_transport(p.transport())
    try:
        store.upsert_account(s.id, client_id="cid")
        with pytest.raises(oauth.OAuthError) as e:
            oauth.start(s.id, "read", listen=False)
        assert e.value.code == "client_secret_required"
        tokens.save_client_secret(s.id, "byok-secret")
        f = oauth.start(s.id, "read", listen=False)
        cb = p.authorize(f["authorize_url"])
        assert oauth.complete(cb["state"], cb["code"])["status"] == "connected"
        assert "byok-secret" not in json.dumps(store.get_account(s.id))
    finally:
        oauth.set_transport(prev)
        registry.unregister(s.id)


# ── actions: reads run, writes wait for confirmation ────────────────────────

def test_read_action_runs_and_is_audited(tmpdb, kc, spec, provider):
    _connect(spec, provider)
    r = actions.invoke(spec.id, "me")
    assert r["status"] == "done" and r["result"]["id"] == "u_1"
    assert store.list_audit()[0]["event"] == "read"


def test_write_needs_write_permission(tmpdb, kc, spec, provider):
    _connect(spec, provider, "read")
    with pytest.raises(actions.ActionError) as e:
        actions.invoke(spec.id, "post_message", {"channel": "general", "text": "hola"})
    assert e.value.code == "write_not_permitted" and e.value.status == 403
    assert provider.messages == []
    assert store.list_audit()[0]["event"] == "write_blocked"


def test_write_is_queued_never_executed_without_confirmation(tmpdb, kc, spec, provider):
    _connect(spec, provider, "write")
    payload = {"channel": "general", "text": "hola equipo"}
    r = actions.invoke(spec.id, "post_message", payload)
    assert r["status"] == "confirmation_required" and provider.messages == []
    assert r["summary"] == "Publicar en #general: hola equipo"
    assert r["payload_hash"] == actions.payload_hash(payload)
    pending = actions.list_actions("pending")
    assert [a["id"] for a in pending] == [r["action_id"]]

    with pytest.raises(actions.ActionError) as bad:
        actions.confirm(r["action_id"], actions.payload_hash({**payload, "text": "otra cosa"}))
    assert bad.value.code == "hash_mismatch" and provider.messages == []

    done = actions.confirm(r["action_id"], r["payload_hash"])
    assert done["status"] == "done" and provider.messages == [payload]
    with pytest.raises(actions.ActionError) as again:
        actions.confirm(r["action_id"], r["payload_hash"])
    assert again.value.code == "not_pending" and len(provider.messages) == 1
    ev = [e["event"] for e in store.list_audit()]
    assert ev[:3] == ["write_done", "write_confirmed", "write_requested"]
    assert "hola equipo" not in _audit_text(tmpdb)          # audit keeps the hash, not the content


def test_write_expiry_reject_and_downgrade(tmpdb, kc, spec, provider):
    _connect(spec, provider, "write")
    old = actions.invoke(spec.id, "post_message", {"channel": "a", "text": "1"},
                         now=time.time() - actions.CONFIRM_TTL - 1)
    with pytest.raises(actions.ActionError) as e:
        actions.confirm(old["action_id"], old["payload_hash"])
    assert e.value.status == 410

    r2 = actions.invoke(spec.id, "post_message", {"channel": "a", "text": "2"})
    assert actions.reject(r2["action_id"])["status"] == "rejected"
    with pytest.raises(actions.ActionError):
        actions.confirm(r2["action_id"], r2["payload_hash"])

    r3 = actions.invoke(spec.id, "post_message", {"channel": "a", "text": "3"})
    store.upsert_account(spec.id, permission="read")           # user downgraded meanwhile
    with pytest.raises(actions.ActionError) as e3:
        actions.confirm(r3["action_id"], r3["payload_hash"])
    assert e3.value.code == "write_not_permitted"
    assert provider.messages == []


def test_client_refuses_hosts_outside_allowlist(tmpdb, kc, spec, provider):
    _connect(spec, provider)
    with pytest.raises(actions.ActionError) as e:
        actions.invoke(spec.id, "evil")
    assert e.value.code == "action_failed"
    assert all(API_HOST in u or AUTH_HOST in u for _m, u in provider.calls)


def test_disconnect_revokes_deletes_token_and_expires_pending(tmpdb, kc, spec, provider):
    _connect(spec, provider, "write")
    r = actions.invoke(spec.id, "post_message", {"channel": "a", "text": "x"})
    acc = oauth.disconnect(spec.id)
    assert acc["status"] == "disconnected" and acc["granted_scopes"] == []
    assert provider.revoked == ["fake-refresh-0002"]
    assert tokens.load_token(spec.id) is None and kc.items == {}
    assert actions.list_actions("pending") == []
    with pytest.raises(actions.ActionError):
        actions.confirm(r["action_id"], r["payload_hash"])
    with pytest.raises(actions.ActionError) as e:
        actions.invoke(spec.id, "me")
    assert e.value.code == "not_connected"


def test_audit_redacts_secret_keys():
    out = store.redact({"access_token": "a", "nested": {"client_secret": "b", "code": "c", "ok": 1},
                        "list": [{"refresh_token": "d"}], "text": "x" * 1000})
    s = json.dumps(out)
    assert all(v not in s for v in ('"a"', '"b"', '"c"', '"d"')) and out["nested"]["ok"] == 1
    assert len(out["text"]) < 400


# ── loopback receiver (real socket, 127.0.0.1 only) ─────────────────────────

def test_loopback_receiver_completes_flow_once(tmpdb, kc, spec, provider):
    store.upsert_account(spec.id, client_id="cid")
    f = oauth.start(spec.id, "read", listen=True)
    fl = oauth._FLOWS[f["state"]]
    assert fl.receiver is not None and fl.receiver._srv.server_address[0] == "127.0.0.1"
    cb = provider.authorize(f["authorize_url"])
    with pytest.raises(urllib.error.HTTPError) as bad:
        urllib.request.urlopen(f"{cb['redirect_uri']}?state=nope&code=x", timeout=5)
    assert bad.value.code == 400
    with urllib.request.urlopen(f"{cb['redirect_uri']}?state={cb['state']}&code={cb['code']}", timeout=5) as r:
        assert r.status == 200
        assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert oauth.flow_status(f["state"])["status"] == "connected"
    assert fl.receiver.wait_closed(5)


# ── keychain shim (no #16/#17 on main) ──────────────────────────────────────

def test_keychain_backend_never_falls_back_to_file(monkeypatch):
    from ops_dashboard import secrets_store
    b = tokens._KeychainBackend()
    monkeypatch.setattr(secrets_store, "set_secret",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("file fallback used")))
    monkeypatch.setattr(secrets_store, "_keychain_enabled", lambda: True)
    monkeypatch.delattr(secrets_store, "_kc_write", raising=False)
    # main without #17: writing would put the token in `security` argv → refuse.
    assert b.available() is False
    with pytest.raises(tokens.TokenStoreUnavailable):
        b.set("connector-token:x", "v")
    written: dict[str, str] = {}
    monkeypatch.setattr(secrets_store, "_kc_write", lambda n, v: written.setdefault(n, v) == v, raising=False)
    assert b.available() is True
    b.set("connector-token:x", "v")
    assert written == {"connector-token:x": "v"}
    monkeypatch.setattr(secrets_store, "_keychain_enabled", lambda: False)
    assert b.available() is False


# ── no telemetry ────────────────────────────────────────────────────────────

def test_no_telemetry_or_hardcoded_destinations():
    """The framework has no outbound destination of its own: every URL comes
    from a registered spec (checked against allowed_hosts). Only loopback
    literals are allowed in the source."""
    import re
    from pathlib import Path
    root = Path(oauth.__file__).parent
    files = [root / f for f in ("connector_registry.py", "connector_oauth.py", "connector_tokens.py",
                                "connector_store.py", "connector_actions.py", "routes_connectors.py")]
    for f in files:
        src = f.read_text(encoding="utf-8")
        for url in re.findall(r"https?://[^\s\"'`)]+", src):
            assert url.startswith(("http://127.0.0.1", "http://{host}", "http://<Host>")), (f.name, url)
        for banned in ("import requests", "urllib.request", "analytics", "telemetry.", "posthog", "sentry_sdk"):
            assert banned not in src, (f.name, banned)


def test_disconnect_cancels_an_abandoned_flow(tmpdb, kc, spec, provider):
    _connect(spec, provider, "read")
    upgrade = oauth.start(spec.id, "write", listen=False)       # user starts "upgrade to write"…
    cb = provider.authorize(upgrade["authorize_url"])
    oauth.disconnect(spec.id)                                    # …then clicks Disconnect
    with pytest.raises(oauth.OAuthError) as late:                # the late callback is refused
        oauth.complete(cb["state"], cb.get("code"), cb.get("error"))
    assert late.value.code == "unknown_state"
    assert store.get_account(spec.id)["status"] == "disconnected"
    assert tokens.load_token(spec.id) is None and kc.items == {}


def test_disconnect_fails_loudly_when_the_keychain_keeps_the_token(tmpdb, kc, spec, provider, monkeypatch):
    from ops_dashboard import secrets_store
    stuck = {"connector-token:x": "still-here"}
    monkeypatch.setattr(secrets_store, "_kc_delete", lambda item: None)          # denied / timed out
    monkeypatch.setattr(secrets_store, "keychain_get", lambda item: stuck.get(item), raising=False)
    b = tokens._KeychainBackend()
    monkeypatch.setattr(b, "available", lambda: True)
    with pytest.raises(tokens.TokenStoreUnavailable):
        b.delete("connector-token:x")
    stuck.clear()
    b.delete("connector-token:x")                                                 # gone: no error

    _connect(spec, provider, "read")

    class _Stuck(tokens.MemoryBackend):
        def delete(self, item):
            raise tokens.TokenStoreUnavailable("keychain_delete_failed")
    _Stuck.items = kc.items
    prev = tokens.use_backend(_Stuck())
    try:
        with pytest.raises(oauth.OAuthError) as e:
            oauth.disconnect(spec.id)
    finally:
        tokens.use_backend(prev)
    assert e.value.code == "keychain_delete_failed"
    assert store.get_account(spec.id)["status"] == "connected"                     # not a false "disconnected"
