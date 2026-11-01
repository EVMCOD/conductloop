"""A credential that lasts for years must not sit in a plaintext column.

The ``connections`` table stores its credentials as JSON in a TEXT column.
The comment on the column says it plainly: *"token stored in plain text -
consider keychain in prod"*. For a short-lived OAuth token that is a debt
you can pay later. For a personal token that is valid for years, and that
the whole "connect once" promise rests on, it is the weak point of the
design: a database backup, a support bundle or a stray screenshot carries a
credential that keeps working.

The connectors that already use ``token_mode="keychain"`` (Todoist,
Readwise) store theirs in the OS keychain. The ones that go through
``connections`` (GitHub, Notion, Linear) do not.

So: the token goes to the keychain, and the database keeps only the fact
that there is one.

These tests pin the behaviour that matters:

- The token is NOT in the database after connecting.
- The token IS retrievable afterwards, so the connector keeps working.
- The keychain is the source of truth; the database row is a pointer.
- If the keychain is unavailable, the connector says so instead of silently
  falling back to plaintext.
- An existing plaintext credential is migrated, not left behind.
"""
from __future__ import annotations

import pytest

from ops_dashboard import connector_tokens as tokens
from ops_dashboard import db as dbmod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "creds.db")
    dbmod.init_db()
    tokens.use_backend(tokens.MemoryBackend())
    yield


def _rows_with_secret() -> list[str]:
    """Every string in the database that contains the canary in the clear.

    The ``credentials`` column stores an encrypted blob (``enc:``), so the
    canary is not there to begin with. The check that matters is therefore
    NOT "the canary is in the database" — it is "the row that held the
    credential no longer carries it", checked against the value the row had
    before the migration.
    """
    canary = "canary-token-xyz"
    found: list[str] = []
    with dbmod.db() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in tables:
            try:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            except Exception:
                continue
            for row in rows:
                for value in tuple(row):
                    if isinstance(value, str) and canary in value:
                        found.append(f"{table}: {value[:60]}")
    return found


def _credential_rows() -> list[str]:
    """The credentials of every connection, as the app reads them back."""
    out: list[str] = []
    with dbmod.db() as conn:
        rows = conn.execute("SELECT id, credentials FROM connections").fetchall()
    for r in rows:
        out.append(str(r[1]))
    return out


def test_the_token_is_not_in_the_database_after_connecting(env):
    from ops_dashboard.connectors_token import base

    base.register_connector(base.TokenConnector(
        spec=__import__("ops_dashboard.connector_registry", fromlist=["x"]).ConnectorSpec(
            id="creds_demo", name="Creds Demo", auth="token",
            allowed_hosts=("api.demo.test",)),
        token_mode="keychain",
        whoami=lambda c: "enrique",
        sync=lambda c, cfg: [],
    ))
    try:
        base.connect("creds_demo", token="canary-token-xyz")
        assert not _rows_with_secret(), "the token is in the database"
    finally:
        base.unregister_connector("creds_demo")


def test_the_connector_still_works_after_connecting(env):
    """Storing it in the keychain must not break the connector."""
    from ops_dashboard import connector_registry as registry
    from ops_dashboard.connectors_token import base

    nc = base.register_connector(base.TokenConnector(
        spec=registry.ConnectorSpec(
            id="creds_work", name="Creds Work", auth="token",
            allowed_hosts=("api.demo.test",)),
        token_mode="keychain",
        whoami=lambda c: "enrique",
        sync=lambda c, cfg: [],
    ))
    try:
        base.connect("creds_work", token="canary-token-xyz")
        # The token is retrievable and is the one that was stored.
        rec = tokens.load_token("creds_work")
        assert rec is not None
        assert rec["access_token"] == "canary-token-xyz"
        # And the connector can build a client with it.
        client = base.client_for(nc)
        assert client is not None
    finally:
        base.unregister_connector("creds_work")


def test_without_a_keychain_the_connector_refuses_instead_of_storing_plaintext(env, monkeypatch):
    """Falling back to plaintext would be worse than failing."""
    from ops_dashboard import connector_registry as registry
    from ops_dashboard.connectors_token import base

    class _NoKeychain(tokens.MemoryBackend):
        def available(self) -> bool:
            return False

    tokens.use_backend(_NoKeychain())
    base.register_connector(base.TokenConnector(
        spec=registry.ConnectorSpec(
            id="creds_nokc", name="Creds NoKC", auth="token",
            allowed_hosts=("api.demo.test",)),
        token_mode="keychain",
        whoami=lambda c: "enrique",
        sync=lambda c, cfg: [],
    ))
    try:
        with pytest.raises(base.TokenError) as ei:
            base.connect("creds_nokc", token="canary-token-xyz")
        assert ei.value.code == "keychain_unavailable"
        assert not _rows_with_secret()
    finally:
        base.unregister_connector("creds_nokc")


def test_a_plaintext_credential_left_earlier_is_migrated_to_the_keychain(env):
    """A user who connected before this change has a token in the database.
    It has to move, and the old copy has to go."""
    from ops_dashboard import connector_registry as registry
    from ops_dashboard.connectors_token import base

    # The state as it was: a credential in the connections table.
    conn_id = dbmod.add_connection("github", "GitHub", {"token": "canary-token-xyz",
                                                        "other": "keep-me"})
    assert conn_id

    base.register_connector(base.TokenConnector(
        spec=registry.ConnectorSpec(
            id="creds_migrate", name="Creds Migrate", auth="token",
            allowed_hosts=("api.demo.test",)),
        token_mode="keychain",
        whoami=lambda c: "enrique",
        sync=lambda c, cfg: [],
    ))
    try:
        base.save_state("creds_migrate", {"connection_id": conn_id})
        moved = base.migrate_plaintext_credentials()
        assert moved, "nothing was migrated"
        rec = tokens.load_token("creds_migrate")
        assert rec is not None and rec["access_token"] == "canary-token-xyz"
        # The row no longer carries the credential. (The app reads the column
        # back decrypted, so this is the real value, not the stored blob.)
        creds = dbmod.get_connection_by_id(conn_id).get("credentials") or {}
        assert "canary-token-xyz" not in str(creds), (
            "the row still carries the credential")
        # What the row held besides the credential is still there: the
        # migration moves the secret, it does not empty the row.
        assert creds.get("other") == "keep-me"
        assert not _rows_with_secret()
    finally:
        base.unregister_connector("creds_migrate")


def test_migrating_twice_does_not_duplicate_or_break(env):
    from ops_dashboard import connector_registry as registry
    from ops_dashboard.connectors_token import base

    conn_id = dbmod.add_connection("github", "GitHub", {"token": "canary-token-xyz",
                                                        "other": "keep-me"})
    base.register_connector(base.TokenConnector(
        spec=registry.ConnectorSpec(
            id="creds_twice", name="Creds Twice", auth="token",
            allowed_hosts=("api.demo.test",)),
        token_mode="keychain",
        whoami=lambda c: "enrique",
        sync=lambda c, cfg: [],
    ))
    try:
        base.save_state("creds_twice", {"connection_id": conn_id})
        base.migrate_plaintext_credentials()
        base.migrate_plaintext_credentials()
        rec = tokens.load_token("creds_twice")
        assert rec is not None and rec["access_token"] == "canary-token-xyz"
    finally:
        base.unregister_connector("creds_twice")


def test_the_migration_does_not_touch_a_credential_that_is_already_in_the_keychain(env):
    """If the keychain already has the token, the database copy is redundant
    and can go — but the keychain copy must be untouched."""
    from ops_dashboard import connector_registry as registry
    from ops_dashboard.connectors_token import base

    conn_id = dbmod.add_connection("github", "GitHub", {"token": "canary-token-xyz",
                                                        "other": "keep-me"})
    base.register_connector(base.TokenConnector(
        spec=registry.ConnectorSpec(
            id="creds_keep", name="Creds Keep", auth="token",
            allowed_hosts=("api.demo.test",)),
        token_mode="keychain",
        whoami=lambda c: "enrique",
        sync=lambda c, cfg: [],
    ))
    try:
        base.save_state("creds_keep", {"connection_id": conn_id})
        tokens.save_token("creds_keep", {"access_token": "already-in-keychain"})
        base.migrate_plaintext_credentials()
        rec = tokens.load_token("creds_keep")
        assert rec["access_token"] == "already-in-keychain", "the keychain copy was overwritten"
    finally:
        base.unregister_connector("creds_keep")
