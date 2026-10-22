"""Shared kit for token / no-auth connectors.

A ``TokenConnector`` wraps a #40 ``ConnectorSpec`` (auth ``token``) and adds:

* **Where the token lives** (``token_mode``):
  - ``connections``: reuse the existing token connection of the same provider
    (``connections`` table, "Conexiones" view, Fernet-encrypted with the key in
    the keychain). GitHub, Linear and Notion use this, so a token is never
    stored twice. Pasting a new token in Conectores creates/updates the
    ``connections`` row named ``conectores``.
  - ``keychain``: OS keychain only (``connector_tokens``), for services that
    had no connection before (Todoist, Readwise).
  - ``none``: public data (RSS).
* **A read-only sync that ingests into the vault** (``documents`` table,
  ``source='<id>'``, ``path='<id>/<kind>/<key>'``, same convention as the OAuth
  connectors in ``connectors_native``) so the brain/search can
  use it. Every ingested item is marked ``untrusted`` in its metadata and
  invisible/control characters are stripped.
* **TokenClient**: https only, only the connector's hosts (or, for RSS, the
  hosts the user configured, which must resolve to public IPs), no redirects,
  bounded response size, the token only in the Authorization header.

Writes are ordinary #40 write actions: they queue in "Pendiente de confirmar"
and only run after the user confirms. Config and sync state (no secrets) are
a small JSON file per connector next to the DB.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .. import connector_actions as actions
from .. import connector_oauth as oauth
from .. import connector_registry as registry
from .. import connector_store as store
from .. import connector_tokens as tokens
from .. import db as _dbmod

TOKEN_MODES = ("connections", "keychain", "none")
MAX_RESPONSE = 5 * 1024 * 1024
MAX_CONTENT = 100_000
MAX_ITEMS = 500
CONNECTIONS_NAME = "conectores"

# Extra audit events (appended at runtime so this kit never edits the shared tuple).
_EVENTS = ("synced", "sync_failed", "data_forgotten")
store.AUDIT_EVENTS = tuple(dict.fromkeys(tuple(store.AUDIT_EVENTS) + _EVENTS))  # type: ignore[assignment]


class TokenError(actions.ActionError):
    pass


@dataclass(frozen=True)
class Item:
    """One vault document. A sync yields ``PARTIAL`` (below) when it stopped
    before the provider's last page, so nothing is pruned from that result."""
    kind: str                      # 'issue' | 'pr' | 'task' | 'entry' | 'highlight' …
    key: str                       # stable id inside the connector
    title: str
    content: str
    url: str = ""
    updated: int = 0               # unix seconds; 0 ⇒ now
    tags: tuple[str, ...] = ()


PARTIAL = Item(kind="__partial__", key="", title="", content="")


def _is_rate_limit(r: httpx.Response) -> bool:
    if r.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in r.headers:
        return True
    return b"rate limit" in (r.content or b"")[:2000].lower()


@dataclass(frozen=True)
class ConfigField:
    key: str
    kind: str                      # 'list' (one value per line) | 'text'
    label_es: str
    label_en: str
    pattern: str = ""              # every value must fullmatch this regex
    max_items: int = 50


@dataclass
class TokenConnector:
    spec: registry.ConnectorSpec
    token_mode: str
    whoami: Callable[[TokenClient], str]                    # account label; raises on a bad token
    sync: Callable[[TokenClient, dict], Iterable[Item]]     # (client, config) -> items
    connections_provider: str = ""
    connections_key: str = "token"
    auth_scheme: str = "Bearer"                  # "" ⇒ Authorization: <token>
    config_fields: tuple[ConfigField, ...] = ()
    hosts_from_config: Callable[[dict], tuple[str, ...]] | None = None
    token_help_es: str = ""
    token_help_en: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    # Kinds whose sync yields the whole current set (e.g. active tasks): after
    # a complete sync, documents of these kinds that were not yielded are
    # removed from the vault (completed/deleted upstream, or filtered out).
    snapshot_kinds: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return self.spec.id


_NATIVE: dict[str, TokenConnector] = {}


def register_connector(nc: TokenConnector) -> TokenConnector:
    if nc.token_mode not in TOKEN_MODES:
        raise registry.RegistryError(f"{nc.id}: bad token_mode {nc.token_mode!r}")
    if nc.spec.auth != "token":
        raise registry.RegistryError(f"{nc.id}: token connectors use auth='token'")
    if nc.token_mode == "connections" and not nc.connections_provider:
        raise registry.RegistryError(f"{nc.id}: connections_provider is required")
    if registry.get(nc.id) is not nc.spec:
        registry.register(nc.spec)
    _NATIVE[nc.id] = nc
    return nc


def unregister_connector(connector_id: str) -> None:
    _NATIVE.pop(connector_id, None)
    registry.unregister(connector_id)


def get(connector_id: str) -> TokenConnector:
    nc = _NATIVE.get(connector_id)
    if nc is None:
        raise TokenError("unknown_connector", connector_id, 404)
    return nc


def all_connectors() -> list[TokenConnector]:
    return [_NATIVE[k] for k in sorted(_NATIVE)]


# ── text hygiene ────────────────────────────────────────────────────────────

_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")


def clean_text(s: Any, limit: int = MAX_CONTENT) -> str:
    s = "" if s is None else str(s)
    s = _INVISIBLE.sub("", s)
    s = "".join(ch for ch in s if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    return s[:limit]


# ── state file (config + last sync; never secrets) ─────────────────────────

def _state_path(connector_id: str) -> Path:
    return Path(_dbmod.DB_PATH).parent / "connectors" / f"{connector_id}.json"


def load_state(connector_id: str) -> dict:
    try:
        data = json.loads(_state_path(connector_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(connector_id: str, state: dict) -> None:
    p = _state_path(connector_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, p)


def validate_config(nc: TokenConnector, raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise TokenError("bad_config", "config must be an object")
    out: dict[str, Any] = {}
    for f in nc.config_fields:
        v = raw.get(f.key)
        if f.kind == "list":
            vals = v if isinstance(v, list) else str(v or "").splitlines()
            vals = [str(x).strip() for x in vals if str(x).strip()]
            if len(vals) > f.max_items:
                raise TokenError("bad_config", f"{f.key}: at most {f.max_items} values")
            if f.pattern:
                bad = [x for x in vals if not re.fullmatch(f.pattern, x)]
                if bad:
                    raise TokenError("bad_config", f"{f.key}: invalid value {bad[0][:80]!r}")
            out[f.key] = list(dict.fromkeys(vals))
        else:
            s = str(v or "").strip()
            if f.pattern and s and not re.fullmatch(f.pattern, s):
                raise TokenError("bad_config", f"{f.key}: invalid value")
            out[f.key] = s[:500]
    return out


def config_of(nc: TokenConnector) -> dict:
    cfg = load_state(nc.id).get("config")
    return validate_config(nc, cfg if isinstance(cfg, dict) else {})


# ── HTTP ────────────────────────────────────────────────────────────────────

_resolver: Callable[[str], list[str]] = lambda host: [  # noqa: E731
    str(ai[4][0]) for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def set_resolver(fn: Callable[[str], list[str]]) -> Callable[[str], list[str]]:
    """Tests replace DNS so user-configured hosts can be checked offline."""
    global _resolver
    prev, _resolver = _resolver, fn
    return prev


def _public_host(host: str) -> None:
    try:
        addrs = _resolver(host)
    except OSError as e:
        raise TokenError("dns_failed", host, 502) from e
    if not addrs:
        raise TokenError("dns_failed", host, 502)
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise TokenError("host_not_public", f"{host} resolves to a non-public address", 400)


class TokenClient:
    def __init__(self, nc: TokenConnector, token: str, user_hosts: tuple[str, ...] = ()) -> None:
        self.nc = nc
        self._token = token
        self._user_hosts = tuple(h.lower() for h in user_hosts)

    def request(self, method: str, url: str, *, headers: dict | None = None, **kw: Any) -> httpx.Response:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not host:
            raise TokenError("host_not_allowed", f"{self.nc.id}: https only", 400)
        if host in self.nc.spec.allowed_hosts:
            pass
        elif host in self._user_hosts:
            _public_host(host)
        else:
            raise TokenError("host_not_allowed", f"{self.nc.id} may not contact {host}", 400)
        h = {"User-Agent": "ConductLoop-connectors", **(headers or {})}
        if self._token:
            # auth_scheme "" ⇒ raw key in the header (e.g. Linear personal API keys).
            h["Authorization"] = f"{self.nc.auth_scheme} {self._token}" if self.nc.auth_scheme else self._token
        with oauth._client() as c:
            with c.stream(method, url, headers=h, **kw) as r:
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_RESPONSE:
                        raise TokenError("response_too_large", f"> {MAX_RESPONSE} bytes", 502)
                return httpx.Response(r.status_code, headers=r.headers, content=bytes(buf), request=r.request)

    def _check(self, r: httpx.Response) -> httpx.Response:
        if r.status_code == 403 and _is_rate_limit(r):
            # GitHub answers 403 (not 429) for primary/secondary rate limits:
            # transient, so the still-valid token must not be marked broken.
            raise TokenError("rate_limited", f"{self.nc.id}: HTTP 403 rate limit", 429)
        if r.status_code in (401, 403):
            raise TokenError("unauthorized", f"{self.nc.id}: HTTP {r.status_code}", 401)
        if r.status_code == 429:
            raise TokenError("rate_limited", f"{self.nc.id}: HTTP 429", 429)
        if 300 <= r.status_code < 400:
            raise TokenError("redirect_refused", f"{self.nc.id}: HTTP {r.status_code}", 502)
        if r.status_code >= 400:
            raise TokenError("http_error", f"{self.nc.id}: HTTP {r.status_code}", 502)
        return r

    def get_json(self, url: str, **kw: Any) -> Any:
        return self._check(self.request("GET", url, **kw)).json()

    def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        r = self._check(self.request("POST", url, json=body, **kw))
        return r.json() if r.content else {}

    def get_text(self, url: str, **kw: Any) -> str:
        return self._check(self.request("GET", url, **kw)).text


# ── tokens ──────────────────────────────────────────────────────────────────

def _connection_token(nc: TokenConnector, connection_id: int) -> str | None:
    from .. import db
    row = db.get_connection_by_id(int(connection_id))
    if not row or row.get("provider") != nc.connections_provider:
        return None
    creds = row.get("credentials") or {}
    tok = creds.get(nc.connections_key) or creds.get("token") or creds.get("api_key")
    return str(tok) if tok else None


def token_for(nc: TokenConnector) -> str:
    if nc.token_mode == "none":
        return ""
    if nc.token_mode == "keychain":
        rec = tokens.load_token(nc.id)
        tok = rec.get("access_token") if rec else None
    else:
        cid = load_state(nc.id).get("connection_id")
        tok = _connection_token(nc, cid) if cid else None
    if not tok:
        store.upsert_account(nc.id, status="error", last_error="token_missing")
        raise TokenError("not_connected", f"{nc.id}: token missing; connect again", 409)
    return tok


def client_for(nc: TokenConnector, token: str | None = None) -> TokenClient:
    hosts = nc.hosts_from_config(config_of(nc)) if nc.hosts_from_config else ()
    return TokenClient(nc, token_for(nc) if token is None else token, hosts)


def connections_for(nc: TokenConnector) -> list[dict]:
    if nc.token_mode != "connections":
        return []
    from .. import db
    return [{"id": c["id"], "name": c["name"], "status": c.get("status")}
            for c in db.list_connections(nc.connections_provider)]


def connect(connector_id: str, *, token: str | None = None, connection_id: int | None = None,
            now: float | None = None) -> dict:
    """Verify the credential with the provider (whoami) *before* storing
    anything, then mark the account connected with permission ``read``."""
    nc = get(connector_id)
    now = time.time() if now is None else now
    via: dict[str, Any] = {"via": nc.token_mode}
    if token is not None:
        token = token.strip()
        if not token or len(token) > 4096 or any(c.isspace() for c in token):
            raise TokenError("bad_token", "paste the token without spaces")
    if nc.token_mode == "none":
        token = ""
    elif nc.token_mode == "connections" and connection_id is not None:
        token = _connection_token(nc, connection_id)
        if token is None:
            raise TokenError("unknown_connection", f"no {nc.connections_provider} connection {connection_id}", 404)
        via["connection_id"] = int(connection_id)
    elif not token:
        raise TokenError("token_required", "paste a token or pick an existing connection")
    if nc.token_mode == "keychain" and not tokens.available():
        raise TokenError("keychain_unavailable", "no OS keychain: the token can't be stored", 503)
    try:
        label = clean_text(nc.whoami(client_for(nc, token or "")), 200)
    except TokenError as e:
        store.upsert_account(nc.id, last_error=e.code)
        store.audit(nc.id, "connect_failed", {"error": e.code})
        raise
    except Exception as e:
        store.audit(nc.id, "connect_failed", {"error": type(e).__name__})
        raise TokenError("connect_failed", type(e).__name__, 502) from e
    state = load_state(nc.id)
    if nc.token_mode == "keychain":
        tokens.save_token(nc.id, {"access_token": token}, now=now)
    elif nc.token_mode == "connections" and connection_id is None:
        from .. import db
        existing = db.get_connection_by_name(nc.connections_provider, CONNECTIONS_NAME)
        if existing:
            db.update_connection(existing["id"], credentials={nc.connections_key: token})
            cid = existing["id"]
        else:
            cid = db.add_connection(nc.connections_provider, CONNECTIONS_NAME, {nc.connections_key: token})
        via["connection_id"] = int(cid)
    if "connection_id" in via:
        state["connection_id"] = via["connection_id"]
    save_state(nc.id, state)
    acc = store.get_account(nc.id)
    store.upsert_account(nc.id, status="connected", permission=acc["permission"] if acc["status"] == "connected"
                         else "read", account_label=label or None, last_error=None, connected_at=int(now))
    store.audit(nc.id, "connected", via)
    return view(nc.id)


def set_permission(connector_id: str, permission: str) -> dict:
    """With a personal token the provider-side scope is whatever the user
    chose when creating it; locally, writes stay off until granted here and
    every write still waits for confirmation."""
    nc = get(connector_id)
    if permission not in registry.PERMISSIONS:
        raise TokenError("bad_permission", "permission must be read or write")
    if store.get_account(nc.id)["status"] != "connected":
        raise TokenError("not_connected", nc.id, 409)
    if not any(a.kind == "write" for a in nc.spec.actions.values()) and permission == "write":
        raise TokenError("no_write_actions", f"{nc.id} has no write actions")
    store.upsert_account(nc.id, permission=permission)
    expired = actions.expire_for_connector(nc.id) if permission == "read" else 0
    store.audit(nc.id, "permission_changed", {"permission": permission, "pending_expired": expired})
    return view(nc.id)


def save_config(connector_id: str, raw: Any) -> dict:
    nc = get(connector_id)
    state = load_state(nc.id)
    state["config"] = validate_config(nc, raw)
    save_state(nc.id, state)
    return view(nc.id)


# ── ingest ──────────────────────────────────────────────────────────────────

def _doc_prefix(connector_id: str) -> str:
    return f"{connector_id}/"


def ingest(nc: TokenConnector, items: Iterable[Item], *, now: float | None = None) -> dict:
    from .. import db
    now = time.time() if now is None else now
    fetched = inserted = 0
    seen: set[str] = set()
    complete = True
    for it in items:
        if it is PARTIAL:
            complete = False
            continue
        if fetched >= MAX_ITEMS:
            complete = False
            break
        fetched += 1
        kind = re.sub(r"[^a-z0-9_-]", "", it.kind.lower())[:24] or "item"
        key = re.sub(r"[^A-Za-z0-9_.:#@-]", "_", it.key)[:200]
        content = clean_text(it.content)
        title = clean_text(it.title, 300)
        meta = {"connector": nc.id, "kind": kind, "url": it.url[:2000] if it.url.startswith("https://") else "",
                "untrusted": True, "tags": [nc.id, kind, *[clean_text(t, 40) for t in it.tags[:10]]]}
        path = f"{nc.id}/{kind}/{key}"
        seen.add(path)
        if db.upsert_document(source=nc.id, path=path, title=title, content=content,
                              metadata=meta, mtime=int(it.updated or now), size=len(content),
                              compare_content=True):
            inserted += 1
    out = {"fetched": fetched, "inserted_or_updated": inserted, "unchanged": fetched - inserted}
    if nc.snapshot_kinds and complete:
        out["removed"] = _prune_snapshot(nc, seen)
    return out


def _prune_snapshot(nc: TokenConnector, seen: set[str]) -> int:
    from .. import db
    removed = 0
    with db.db() as conn:
        for kind in nc.snapshot_kinds:
            prefix = f"{nc.id}/{kind}/"
            rows = conn.execute("SELECT id, path FROM documents WHERE source = ? AND substr(path, 1, ?) = ?",
                                (nc.id, len(prefix), prefix)).fetchall()
            for r in rows:
                if r[1] not in seen:
                    conn.execute("DELETE FROM documents WHERE id = ?", (r[0],))
                    removed += 1
    return removed


def run_sync(connector_id: str, *, now: float | None = None) -> dict:
    """Read-only: pull from the provider and upsert into the vault."""
    nc = get(connector_id)
    if store.get_account(nc.id)["status"] != "connected":
        raise TokenError("not_connected", nc.id, 409)
    now = time.time() if now is None else now
    try:
        res = ingest(nc, nc.sync(client_for(nc), config_of(nc)), now=now)
    except TokenError as e:
        store.audit(nc.id, "sync_failed", {"error": e.code})
        if e.code == "unauthorized":
            store.upsert_account(nc.id, status="error", last_error="unauthorized")
        raise
    except Exception as e:
        store.audit(nc.id, "sync_failed", {"error": type(e).__name__})
        raise TokenError("sync_failed", type(e).__name__, 502) from e
    state = load_state(nc.id)
    state["last_sync"] = {"at": int(now), **res}
    save_state(nc.id, state)
    store.audit(nc.id, "synced", res)
    return res


def forget(connector_id: str) -> int:
    """Delete everything this connector ingested into the vault."""
    nc = get(connector_id)
    with _dbmod.db() as conn:
        cur = conn.execute("DELETE FROM documents WHERE source = ? AND substr(path, 1, ?) = ?",
                           (nc.id, len(_doc_prefix(nc.id)), _doc_prefix(nc.id)))
        n = cur.rowcount or 0
    state = load_state(nc.id)
    state.pop("last_sync", None)
    save_state(nc.id, state)
    store.audit(nc.id, "data_forgotten", {"documents": n})
    return n


def doc_count(connector_id: str) -> int:
    with _dbmod.db() as conn:
        r = conn.execute("SELECT COUNT(*) FROM documents WHERE source = ? AND substr(path, 1, ?) = ?",
                         (connector_id, len(_doc_prefix(connector_id)), _doc_prefix(connector_id))).fetchone()
    return int(r[0] if r else 0)


# ── action helpers for connector modules ────────────────────────────────────

def token_action(nc_id: str, fn: Callable[[TokenClient, dict], dict]) -> Callable[..., dict]:
    """Adapt fn(token_client, payload) to a #40 action handler(client, payload)
    (read or write: writes only reach it after the user confirms)."""
    def handler(_client: Any, payload: dict) -> dict:
        return fn(client_for(get(nc_id)), payload)
    return handler


def sync_action(nc_id: str) -> Callable[..., dict]:
    def handler(_client: Any, _payload: dict) -> dict:
        return run_sync(nc_id)
    return handler


# ── view ────────────────────────────────────────────────────────────────────

def view(connector_id: str) -> dict:
    nc = get(connector_id)
    state = load_state(nc.id)
    linked = state.get("connection_id") if nc.token_mode == "connections" else None
    linked_name = None
    if linked:
        for c in connections_for(nc):
            if c["id"] == linked:
                linked_name = c["name"]
    if nc.token_mode == "keychain":
        stored = tokens.load_token(nc.id) is not None
    elif nc.token_mode == "connections":
        stored = bool(linked_name)
    else:
        stored = True
    return {
        "id": nc.id, "token_mode": nc.token_mode, "credential_stored": stored,
        "connections": connections_for(nc), "linked_connection": linked_name,
        "config": config_of(nc), "last_sync": state.get("last_sync"), "documents": doc_count(nc.id),
        "config_fields": [{"key": f.key, "kind": f.kind, "label_es": f.label_es, "label_en": f.label_en}
                          for f in nc.config_fields],
        "token_help": {"es": nc.token_help_es, "en": nc.token_help_en},
        "has_writes": any(a.kind == "write" for a in nc.spec.actions.values()),
    }
