"""Connector registry: declarative specs for OAuth/token connectors.

A ``ConnectorSpec`` says *what* a connector may touch, never *how much*:

* ``read_scopes`` / ``write_scopes`` — minimal provider scopes. A connector is
  connected with permission ``read`` (only read scopes are requested) or
  ``write`` (read + write scopes). Upgrading needs a new consent.
* ``actions`` — every operation the agent/UI can run, each tagged ``read`` or
  ``write``. Write actions never run directly: they queue a pending action that
  the user confirms in the Conectores page (see ``connector_actions``).
* ``allowed_hosts`` — the only hosts the framework will talk to for this
  connector (token exchange, refresh, revoke, API calls). No telemetry: there is
  no other outbound destination.

This PR ships the framework only; native connectors (Google, Microsoft, Slack,
Notion…) register themselves later with ``register()``. Tests register a fake.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

_ID_RE = re.compile(r"^[a-z][a-z0-9_\-]{1,39}$")
PERMISSIONS = ("read", "write")


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ActionSpec:
    name: str
    kind: str                                   # 'read' | 'write'
    description: str
    handler: Callable[..., dict[str, Any]]      # handler(client, payload) -> dict
    summarize: Callable[[dict[str, Any]], str] | None = None   # human text for the confirm card


@dataclass(frozen=True)
class ConnectorSpec:
    id: str
    name: str
    auth: str                                   # 'oauth_pkce' | 'token' | 'mcp'
    allowed_hosts: tuple[str, ...]
    authorize_url: str = ""
    token_url: str = ""
    revoke_url: str = ""
    read_scopes: tuple[str, ...] = ()
    write_scopes: tuple[str, ...] = ()
    scope_separator: str = " "
    default_client_id: str = ""                 # embeddable public client id (option A); "" ⇒ BYOK only
    client_secret_required: bool = False        # e.g. Notion public integrations
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    actions: dict[str, ActionSpec] = field(default_factory=dict)
    docs_url: str = ""
    # Loopback redirect: "" ⇒ 127.0.0.1; "localhost" for providers that only
    # document localhost (Slack desktop redirects, Microsoft Entra). The
    # receiver always binds 127.0.0.1. ``redirect_port`` 0 ⇒ ephemeral; a fixed
    # port for providers that compare the full redirect, port included (Slack).
    redirect_host: str = ""
    redirect_port: int = 0

    def scopes_for(self, permission: str) -> list[str]:
        if permission not in PERMISSIONS:
            raise RegistryError(f"unknown permission {permission!r}")
        out = list(self.read_scopes)
        if permission == "write":
            out += [s for s in self.write_scopes if s not in out]
        return out

    def host_allowed(self, url: str) -> bool:
        u = urlparse(url)
        return u.scheme == "https" and (u.hostname or "") in self.allowed_hosts

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "auth": self.auth,
            "read_scopes": list(self.read_scopes), "write_scopes": list(self.write_scopes),
            "byok_required": not self.default_client_id,
            "client_secret_required": self.client_secret_required,
            "actions": [{"name": a.name, "kind": a.kind, "description": a.description}
                        for a in self.actions.values()],
            "docs_url": self.docs_url,
        }


_REGISTRY: dict[str, ConnectorSpec] = {}


def _validate(spec: ConnectorSpec) -> None:
    if not _ID_RE.match(spec.id):
        raise RegistryError(f"invalid connector id {spec.id!r}")
    if spec.auth not in ("oauth_pkce", "token", "mcp"):
        raise RegistryError(f"{spec.id}: unknown auth {spec.auth!r}")
    # MCP servers (mcp_hub) talk through their own transport (stdio or a
    # single configured URL), never through ConnectorClient.
    if not spec.allowed_hosts and spec.auth != "mcp":
        raise RegistryError(f"{spec.id}: allowed_hosts is required")
    if spec.auth == "oauth_pkce":
        for label, url in (("authorize_url", spec.authorize_url), ("token_url", spec.token_url)):
            if not spec.host_allowed(url):
                raise RegistryError(f"{spec.id}: {label} must be https on an allowed host")
        if spec.revoke_url and not spec.host_allowed(spec.revoke_url):
            raise RegistryError(f"{spec.id}: revoke_url must be https on an allowed host")
    for name, a in spec.actions.items():
        if a.name != name or a.kind not in PERMISSIONS:
            raise RegistryError(f"{spec.id}: bad action {name!r}")
    if any(a.kind == "write" for a in spec.actions.values()) and spec.auth == "oauth_pkce" \
            and not spec.write_scopes:
        raise RegistryError(f"{spec.id}: write actions need write_scopes")
    if spec.redirect_host not in ("", "127.0.0.1", "localhost"):
        raise RegistryError(f"{spec.id}: redirect_host must be 127.0.0.1 or localhost")
    if spec.redirect_port and not 1024 <= spec.redirect_port <= 65535:
        raise RegistryError(f"{spec.id}: redirect_port must be 0 or 1024-65535")


def register(spec: ConnectorSpec, *, replace: bool = False) -> ConnectorSpec:
    _validate(spec)
    if spec.id in _REGISTRY and _REGISTRY[spec.id] is not spec and not replace:
        raise RegistryError(f"connector {spec.id!r} already registered")
    _REGISTRY[spec.id] = spec
    return spec


def unregister(connector_id: str) -> None:
    _REGISTRY.pop(connector_id, None)


def get(connector_id: str) -> ConnectorSpec | None:
    return _REGISTRY.get(connector_id)


def all_specs() -> list[ConnectorSpec]:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]
