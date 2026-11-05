"""Native OAuth connectors (Google, Slack, Notion, Microsoft 365…) on top of the
connectors framework (#40).

Each connector lives in **its own module** in this package and calls
``connector_registry.register()`` at import time; ``load_all()`` imports every
module here, so adding a connector never edits a shared registry line.
This package is imported once from the end of ``routes_connectors``.

Besides the specs it adds local routes: ``GET /api/connectors-sync`` (which
connectors can sync, and which share a BYOK app), ``POST
/api/connectors-client-group/<group>`` (reuse one connector's client ID/secret
for its siblings) and ``POST /api/connectors-sync/<id>``, which runs the connector's ``sync`` read action (fetch → ingest into the local
vault / agenda). It reuses the framework's local-only guard. Nothing here talks
to any host other than the connector's own ``allowed_hosts``.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any

log = logging.getLogger("conductloop.connectors.native")
_LOADED: list[str] = []

# The modules are named here on purpose. A frozen build does not ship the
# package as files: its modules live inside the PYZ archive, so walking the
# folder finds nothing there. The connectors used to register themselves by
# walking it, and in the frozen app the walk came back empty — no connector
# registered itself, the page showed zero connectors, and nothing raised.
# The app that ships is the app the user runs.
#
# A test walks the folder and fails when this list and the folder drift
# apart, so a connector added later is not forgotten here.
_MODULES = (
    "gmail",
    "google_calendar",
    "google_drive",
    "microsoft365",
    "slack",
)
# Connectors that may share one BYOK OAuth app (same client ID/secret) while
# keeping one token per connector, e.g. {"google": ["google_calendar", "gmail", …]}.
CLIENT_GROUPS: dict[str, list[str]] = {}


def join_client_group(group: str, connector_id: str) -> None:
    ids = CLIENT_GROUPS.setdefault(group, [])
    if connector_id not in ids:
        ids.append(connector_id)


def share_client(group: str, source_id: str) -> list[str]:
    """Copy the BYOK client ID (SQLite, public) and client secret (keychain
    only) of ``source_id`` to the other connectors of ``group``. Tokens are
    untouched: each connector keeps its own consent, scopes and keychain item."""
    from .. import connector_actions as actions
    from .. import connector_registry as registry
    from .. import connector_store as store
    from .. import connector_tokens as tokens

    members = CLIENT_GROUPS.get(group) or []
    if source_id not in members:
        raise actions.ActionError("not_in_group", f"{source_id} is not in {group}", 400)
    cid = store.get_account(source_id).get("client_id")
    if not cid:
        raise actions.ActionError("client_id_required", "save the client ID on one connector first", 409)
    spec = registry.get(source_id)
    secret = tokens.load_client_secret(source_id) if spec and spec.client_secret_required else None
    if spec and spec.client_secret_required and not secret:
        raise actions.ActionError("client_secret_required", "save the client secret first", 409)
    done = []
    for dst in members:
        if dst == source_id or registry.get(dst) is None:
            continue
        current = store.get_account(dst).get("client_id")
        if current and current != cid:
            # Another app is already configured there; its refresh token only
            # works with that client, so overwriting would break it later.
            continue
        if secret:
            tokens.save_client_secret(dst, secret)
        store.upsert_account(dst, client_id=cid)
        store.audit(dst, "client_saved", {"client_secret_set": bool(secret), "shared_from": source_id})
        done.append(dst)
    return done


def load_all() -> list[str]:
    """Import (and so register) every connector module in this package."""
    for name in _MODULES:
        if name in _LOADED:
            continue
        try:
            importlib.import_module(f"{__name__}.{name}")
            _LOADED.append(name)
        except Exception as e:  # one broken connector must not take the page down
            log.warning("connector module %s failed to load: %s", name, type(e).__name__)
    return list(_LOADED)


def _install_routes() -> None:
    from .. import connector_actions as actions
    from .. import connector_registry as registry
    from .. import routes_connectors as rc
    from ..routing import router

    @router.get("/api/connectors-sync")
    @rc._guarded
    def connector_sync_list(handler: Any) -> None:
        """Connectors that can ingest into the vault (have a ``sync`` action)."""
        rc._send_json(handler, 200, {"ok": True, "connectors": [
            s.id for s in registry.all_specs() if "sync" in s.actions], "client_groups": CLIENT_GROUPS})

    @router.post("/api/connectors-client-group/<group>")
    @rc._guarded
    def connector_share_client(handler: Any, group: str) -> None:
        """Reuse one connector's BYOK app (client ID + secret) for its group."""
        data = rc._body(handler)
        rc._send_json(handler, 200, {"ok": True, "shared_with": share_client(group, str(data.get("from") or ""))})

    @router.post("/api/connectors-sync/<connector_id>")
    @rc._guarded
    def connector_sync(handler: Any, connector_id: str) -> None:
        """Run the connector's ``sync`` read action now (ingest into the vault)."""
        spec = registry.get(connector_id)
        if spec is None or "sync" not in spec.actions:
            raise actions.ActionError("unknown_connector", connector_id, 404)
        payload = rc._body(handler)
        rc._send_json(handler, 200, actions.invoke(connector_id, "sync", payload))


load_all()
_install_routes()


def _register_token_verifiers() -> None:
    """How to check a personal token for each connector that accepts one.

    A connector that offers both the OAuth flow and a personal token needs
    to be able to check a pasted token against its provider before storing
    it. The check calls only the connector's own identity endpoint.
    """
    from .. import connector_oauth as oauth

    oauth.register_token_verifier("slack", oauth._slack_token_label)
    oauth.register_token_verifier("microsoft365", oauth._microsoft_token_label)


_register_token_verifiers()
