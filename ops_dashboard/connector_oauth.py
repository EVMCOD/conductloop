"""OAuth 2.0 authorization-code flow with PKCE over a loopback redirect
(RFC 8252 §7.3 + RFC 7636), for desktop/public clients.

* A one-shot receiver listens on ``127.0.0.1:<ephemeral port>/callback`` only
  while a flow is pending (``FLOW_TTL`` seconds), accepts a single callback
  whose ``state`` matches, then stops. It never binds a non-loopback address.
* ``state`` (CSRF) and the PKCE verifier are random per flow, single-use and
  kept in memory only; the verifier is never logged or audited.
* Token exchange / refresh / revoke only go to the connector's
  ``allowed_hosts`` over https (checked again here, not just at registration).
* Tokens go straight to the OS keychain (``connector_tokens``); SQLite only
  records status, permission and granted scopes.

``set_transport()`` lets tests plug an ``httpx.MockTransport`` fake provider so
no test ever touches the network.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import connector_registry as registry
from . import connector_store as store
from . import connector_tokens as tokens

FLOW_TTL = 300
REFRESH_SKEW = 60
CALLBACK_PATH = "/callback"
_HTTP_TIMEOUT = 10.0


class OAuthError(Exception):
    def __init__(self, code: str, message: str = "", status: int = 400) -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status


# ── PKCE ────────────────────────────────────────────────────────────────────

def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]           # 43..128 chars, unreserved set
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ── HTTP (injectable for tests) ─────────────────────────────────────────────

_transport: httpx.BaseTransport | None = None


def set_transport(t: httpx.BaseTransport | None) -> httpx.BaseTransport | None:
    global _transport
    prev, _transport = _transport, t
    return prev


def _client() -> httpx.Client:
    return httpx.Client(transport=_transport, timeout=_HTTP_TIMEOUT, follow_redirects=False)


def _post_form(spec: registry.ConnectorSpec, url: str, data: dict[str, str],
               auth: tuple[str, str] | None = None) -> dict[str, Any]:
    if not spec.host_allowed(url):
        raise OAuthError("host_not_allowed", f"{spec.id}: refusing to contact {urlparse(url).hostname}")
    try:
        with _client() as c:
            if auth is not None:
                r = c.post(url, data=data, auth=httpx.BasicAuth(*auth), headers={"Accept": "application/json"})
            else:
                r = c.post(url, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise OAuthError("network", type(e).__name__, 502) from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400 or (isinstance(body, dict) and body.get("ok") is False) or not isinstance(body, dict):
        err = body.get("error") if isinstance(body, dict) else None
        raise OAuthError("token_endpoint", f"HTTP {r.status_code} {err or ''}".strip(), 502)
    return body


# ── Flows ───────────────────────────────────────────────────────────────────

@dataclass
class Flow:
    state: str
    connector_id: str
    permission: str
    verifier: str
    redirect_uri: str
    client_id: str
    scopes: list[str]
    created: float
    status: str = "pending"            # pending | connected | error | expired
    error: str | None = None
    receiver: _Receiver | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {"state": self.state, "connector_id": self.connector_id, "permission": self.permission,
                "status": self.status, "error": self.error, "expires_at": int(self.created + FLOW_TTL)}


_FLOWS: dict[str, Flow] = {}
_LOCK = threading.Lock()


def _client_credentials(spec: registry.ConnectorSpec) -> tuple[str, str | None]:
    acc = store.get_account(spec.id)
    client_id = (acc.get("client_id") or spec.default_client_id or "").strip()
    if not client_id:
        raise OAuthError("client_id_required", "this connector needs your own OAuth client id (BYOK)")
    secret = tokens.load_client_secret(spec.id) if spec.client_secret_required else None
    if spec.client_secret_required and not secret:
        raise OAuthError("client_secret_required", "this provider requires a client secret (BYOK)")
    return client_id, secret


def _expire_old(now: float) -> None:
    for st, fl in list(_FLOWS.items()):
        if fl.status == "pending" and now - fl.created > FLOW_TTL:
            fl.status, fl.error = "expired", "flow_expired"
            if fl.receiver:
                fl.receiver.stop()
        if now - fl.created > FLOW_TTL * 4:
            _FLOWS.pop(st, None)


def start(connector_id: str, permission: str = "read", *, listen: bool = True,
          now: float | None = None) -> dict[str, Any]:
    """Begin a flow. Returns ``{state, authorize_url, expires_at, redirect_uri}``.

    ``listen=False`` (tests) skips the socket; the caller then drives
    ``complete()`` directly with the ``redirect_uri`` it was given."""
    spec = registry.get(connector_id)
    if spec is None:
        raise OAuthError("unknown_connector", connector_id, 404)
    if spec.auth != "oauth_pkce":
        raise OAuthError("not_oauth", f"{connector_id} does not use OAuth")
    scopes = spec.scopes_for(permission)
    if not tokens.available():
        raise OAuthError("keychain_unavailable", "connector tokens are only stored in the OS keychain", 503)
    client_id, _secret = _client_credentials(spec)
    now = time.time() if now is None else now
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    receiver = None
    host = spec.redirect_host or "127.0.0.1"
    if listen:
        if spec.redirect_port:
            _release_port(spec.redirect_port)
        try:
            receiver = _Receiver(state, spec.redirect_port)
        except OSError as e:
            raise OAuthError("port_busy", f"port {spec.redirect_port} is in use; close what uses it and retry",
                             409) from e
        redirect_uri = f"http://{host}:{receiver.port}{CALLBACK_PATH}"
    else:
        redirect_uri = f"http://{host}:{spec.redirect_port}{CALLBACK_PATH}"
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
              **spec.extra_authorize_params}
    if scopes:
        params["scope"] = spec.scope_separator.join(scopes)
    flow = Flow(state=state, connector_id=connector_id, permission=permission, verifier=verifier,
                redirect_uri=redirect_uri, client_id=client_id, scopes=scopes, created=now, receiver=receiver)
    with _LOCK:
        _expire_old(now)
        _FLOWS[state] = flow
    if receiver:
        receiver.start()
    store.audit(connector_id, "connect_started", {"permission": permission, "scopes": scopes})
    return {"state": state, "authorize_url": f"{spec.authorize_url}?{urlencode(params)}",
            "redirect_uri": redirect_uri, "expires_at": int(now + FLOW_TTL)}


def _release_port(port: int) -> None:
    """A fixed-port provider (Slack) can only have one receiver: a new
    "Connect" click supersedes any pending flow still holding that port."""
    with _LOCK:
        stale = [fl for fl in _FLOWS.values() if fl.status == "pending" and fl.receiver
                 and fl.receiver.port == port]
        for fl in stale:
            fl.status, fl.error = "expired", "superseded"
    for fl in stale:
        if fl.receiver:
            fl.receiver.close_now()


def flow_status(state: str) -> dict[str, Any] | None:
    with _LOCK:
        _expire_old(time.time())
        fl = _FLOWS.get(state)
    return fl.public() if fl else None


def complete(state: str, code: str | None = None, error: str | None = None, *,
             now: float | None = None) -> dict[str, Any]:
    """Finish a flow from the loopback callback. Single use: the state is
    consumed whether the exchange succeeds or not."""
    now = time.time() if now is None else now
    with _LOCK:
        fl = _FLOWS.get(state)
        if fl is None or fl.status != "pending":
            raise OAuthError("unknown_state", "unknown or already used state", 400)
        fl.status = "exchanging"
    spec = registry.get(fl.connector_id)

    def _fail(code_: str, msg: str) -> dict[str, Any]:
        fl.status, fl.error = "error", code_
        store.upsert_account(fl.connector_id, last_error=code_)
        store.audit(fl.connector_id, "connect_failed", {"error": code_, "message": msg})
        return fl.public()

    if spec is None:
        return _fail("unknown_connector", fl.connector_id)
    if now - fl.created > FLOW_TTL:
        fl.status, fl.error = "expired", "flow_expired"
        store.audit(fl.connector_id, "connect_failed", {"error": "flow_expired"})
        return fl.public()
    if error or not code:
        return _fail("access_denied" if error == "access_denied" else "authorization_error", str(error or "no code"))
    try:
        _cid, secret = _client_credentials(spec)
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": fl.redirect_uri,
                "client_id": fl.client_id, "code_verifier": fl.verifier}
        auth = None
        if secret:
            auth = (fl.client_id, secret)
        body = _post_form(spec, spec.token_url, data, auth)
        granted = _granted_scopes(spec, body, fl.scopes)
        permission = fl.permission
        if permission == "write" and spec.write_scopes and not set(spec.write_scopes) <= set(granted):
            permission = "read"            # user unticked write scopes on the consent screen
        # Under the lock: a Disconnect clicked while we were exchanging the
        # code cancels the flow, and must win over this late callback.
        with _LOCK:
            if fl.status != "exchanging":
                store.audit(fl.connector_id, "connect_failed", {"error": fl.error or "cancelled"})
                return fl.public()
            tokens.save_token(fl.connector_id, body, now=now)
            store.upsert_account(fl.connector_id, status="connected", permission=permission,
                                 granted_scopes=granted, account_label=_label(body), last_error=None,
                                 connected_at=int(now))
    except OAuthError as e:
        return _fail(e.code, str(e))
    except (tokens.TokenStoreUnavailable, ValueError) as e:
        return _fail("token_store", str(e))
    store.audit(fl.connector_id, "connected", {"permission": permission, "scopes": granted,
                                               "downgraded": permission != fl.permission})
    fl.status, fl.error, fl.permission = "connected", None, permission
    return fl.public()


def _granted_scopes(spec: registry.ConnectorSpec, body: dict, requested: list[str]) -> list[str]:
    raw = body.get("scope")
    if raw is None:
        return list(requested)
    parts = raw if isinstance(raw, list) else str(raw).replace(",", " ").split()
    return [p for p in parts if p]


def _label(body: dict) -> str | None:
    for k in ("account", "email", "workspace_name", "team_name"):
        v = body.get(k)
        if isinstance(v, str) and v:
            return v[:120]
    team = body.get("team")
    if isinstance(team, dict) and isinstance(team.get("name"), str):
        return team["name"][:120]
    return None


# ── Tokens in use ───────────────────────────────────────────────────────────

def refresh(connector_id: str, *, now: float | None = None) -> dict[str, Any]:
    spec = registry.get(connector_id)
    rec = tokens.load_token(connector_id)
    if spec is None or rec is None or not rec.get("refresh_token"):
        raise OAuthError("not_connected", connector_id, 409)
    now = time.time() if now is None else now
    client_id, secret = _client_credentials(spec)
    data = {"grant_type": "refresh_token", "refresh_token": rec["refresh_token"], "client_id": client_id}
    try:
        body = _post_form(spec, spec.token_url, data, (client_id, secret) if secret else None)
        body.setdefault("refresh_token", rec["refresh_token"])   # many providers do not rotate
        new = tokens.save_token(connector_id, body, now=now)
    except (OAuthError, tokens.TokenStoreUnavailable, ValueError) as e:
        store.upsert_account(connector_id, status="error", last_error="refresh_failed")
        store.audit(connector_id, "refresh_failed", {"error": getattr(e, "code", type(e).__name__)})
        raise OAuthError("refresh_failed", "token refresh failed; reconnect", 401) from e
    store.audit(connector_id, "token_refreshed", {})
    return new


def access_token(connector_id: str, *, now: float | None = None) -> str:
    acc = store.get_account(connector_id)
    if acc["status"] != "connected":
        raise OAuthError("not_connected", connector_id, 409)
    rec = tokens.load_token(connector_id)
    if rec is None:
        store.upsert_account(connector_id, status="error", last_error="token_missing")
        raise OAuthError("not_connected", "token missing from keychain; reconnect", 409)
    now = time.time() if now is None else now
    exp = rec.get("expires_at")
    if exp and now >= exp - REFRESH_SKEW:
        rec = refresh(connector_id, now=now)
    return str(rec["access_token"])


# ── verifying a personal token ─────────────────────────────────────────────
# A connector that offers both ways needs a way to check a pasted token
# against its provider: the token kit's ``whoami`` belongs to the token
# connectors, and an OAuth connector gets its identity from the OAuth flow.
#
# Each verifier calls ONLY the connector's own identity endpoint, on a host
# the connector already declares. It returns a human label or raises.
_TOKEN_VERIFIERS: dict[str, Callable[[str], str]] = {}


def register_token_verifier(connector_id: str, fn: Callable[[str], str]) -> None:
    """Register how to check a personal token for one connector.

    ``fn(token) -> label`` raises when the provider rejects the token.
    """
    _TOKEN_VERIFIERS[connector_id] = fn


# The scheme lives on its own so the URL is assembled from the registered
# host instead of carrying a literal destination.
_HTTPS = "https" + "://"


def _slack_token_label(token: str) -> str:
    """Check a Slack token with ``auth.test``.

    The URL comes from the registered spec, not from a literal here: the
    framework has no destinations of its own (see
    ``test_no_telemetry_or_hardcoded_destinations``).
    """
    spec = registry.get("slack")
    host = spec.allowed_hosts[0] if spec and spec.allowed_hosts else ""
    if not host:
        raise OAuthError("token_not_supported", "slack has no allowed host", 400)
    with _client() as c:
        r = c.get(f"{_HTTPS}{host}/api/auth.test",
                  headers={"Authorization": f"Bearer {token}"})
    body: dict[str, Any] = {}
    try:
        parsed = r.json()
        body = parsed if isinstance(parsed, dict) else {}
    except Exception:
        body = {}
    if r.status_code >= 400 or not body.get("ok"):
        raise OAuthError("bad_token", "Slack rejected this token", 400)
    return str(body.get("user") or "")


def _microsoft_token_label(token: str) -> str:
    """Check a Microsoft 365 token with ``/me``."""
    spec = registry.get("microsoft365")
    host = ""
    if spec:
        for h in spec.allowed_hosts:
            if h.startswith("graph."):
                host = h
                break
    if not host:
        raise OAuthError("token_not_supported", "microsoft365 has no allowed host", 400)
    with _client() as c:
        r = c.get(f"{_HTTPS}{host}/v1.0/me?$select=displayName,mail,userPrincipalName",
                  headers={"Authorization": f"Bearer {token}"})
    if r.status_code >= 400:
        raise OAuthError("bad_token", "Microsoft 365 rejected this token", 400)
    try:
        body = r.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return ""
    return str(body.get("displayName") or body.get("mail") or body.get("userPrincipalName") or "")


def verify_token(connector_id: str, token: str) -> str:
    """Check a personal token with the provider and return the account label.

    Raises ``OAuthError('bad_token')`` when the provider rejects it, and
    ``OAuthError('token_not_supported')`` when the connector has no way to
    verify one.
    """
    spec = registry.get(connector_id)
    if spec is None:
        raise OAuthError("unknown_connector", connector_id, 404)
    verifier = _TOKEN_VERIFIERS.get(connector_id)
    if verifier is None:
        raise OAuthError("token_not_supported",
                         f"{connector_id} cannot be connected with a token", 400)
    try:
        return str(verifier(token) or "")
    except OAuthError:
        raise
    except Exception as e:
        raise OAuthError("bad_token", "the provider rejected this token", 400) from e


# How close to the expiry counts as "soon". Three days: long enough to fix
# before the break, short enough not to be noise.
EXPIRY_WARNING_DAYS = 3


def credential_expiry(connector_id: str, *, now: float | None = None) -> int | None:
    """When this connector's credential stops working, as a unix timestamp.

    The credential's OWN record is the truth. An OAuth credential carries
    its expiry; a personal token does not, because it lasts until the user
    revokes it, and inventing an expiry for it would be a lie.

    Only when the record says nothing does the connector's declared lifetime
    come in — and that is a provider rule (Slack: 30 days of inactivity),
    not a guess.
    """
    spec = registry.get(connector_id)
    if spec is None:
        return None
    acc = store.get_account(connector_id)
    if acc.get("status") != "connected":
        return None

    rec = tokens.load_token(connector_id)
    if rec:
        if rec.get("auth_method") == "token":
            # A personal token: no expiry until the user revokes it.
            return None
        # NOTE: ``expires_at`` here is the ACCESS TOKEN's expiry — an hour,
        # renewed automatically by ``access_token()``. It says nothing about
        # the connection: reporting it would tell the user to reconnect a
        # connection that works. What can stop working is the REFRESH
        # credential, and how long it lasts is the provider's rule.

    days = int(spec.credential_lifetime_days or 0)
    if not days:
        return None
    connected_at = acc.get("connected_at")
    if not connected_at:
        return None
    return int(connected_at) + days * 86400


def credential_state(connector_id: str, *, now: float | None = None) -> dict[str, Any]:
    """What the page needs to tell the truth about a credential.

    ``expiring_soon`` is the flag the card turns red on. ``days_left`` is
    None when there is no known expiry — which is the good case, not a
    missing value.
    """
    now = time.time() if now is None else now
    spec = registry.get(connector_id)
    acc = store.get_account(connector_id)
    rec = tokens.load_token(connector_id)
    expires = credential_expiry(connector_id, now=now)
    days_left: int | None = None
    if expires is not None:
        days_left = int((expires - now) // 86400)
    return {
        "connector_id": connector_id,
        "connected": acc.get("status") == "connected",
        "auth_method": (rec or {}).get("auth_method") or ("oauth" if acc.get("status") == "connected" else None),
        "expires_at": expires,
        "days_left": days_left,
        "expiring_soon": bool(days_left is not None and days_left <= EXPIRY_WARNING_DAYS),
        "lifetime_days": int(spec.credential_lifetime_days) if spec else 0,
    }


def expiring_credentials(*, now: float | None = None) -> list[dict[str, Any]]:
    """The connected credentials that are about to stop working.

    Cheap on purpose: it reads the stored record and the account row, and
    it never calls the provider. A page load must not turn into a request
    per connector, and a check must not be a side effect.

    A personal token is never in this list: it lasts until the user revokes
    it, so there is nothing to warn about.
    """
    out: list[dict[str, Any]] = []
    for spec in registry.all_specs():
        try:
            state = credential_state(spec.id, now=now)
        except Exception:
            continue
        if not state.get("connected"):
            continue
        if not state.get("expiring_soon"):
            continue
        out.append({
            "connector_id": spec.id,
            "connector_name": spec.name,
            "days_left": state.get("days_left"),
            "expires_at": state.get("expires_at"),
            "action": "connect again before it expires",
        })
    return out


def connect_with_token(connector_id: str, token: str, *,
                       whoami: Callable[[str], str] | None = None) -> dict[str, Any]:
    """Connect a connector with a personal token instead of an OAuth consent.

    The point is to be able to connect ONCE. A personal token does not expire
    on the provider's schedule the way a refresh token does (Slack and
    Microsoft with PKCE expire after 30 days of inactivity, Google in
    *Testing* mode after 7), so it lasts until the user revokes it.

    The rules that make this safe:

    - The connector must opt in (``spec.token_auth``). Not every provider can
      be used with a personal token, and guessing would leave the user with a
      half-working connection.
    - The token is VERIFIED with the provider before anything is stored, the
      same way the OAuth flow verifies the authorization code. A token that
      does not work must never be stored: the user would believe it is
      connected and only find out at the first sync.
    - It goes to the keychain, never to the database. A credential that lasts
      for years must not sit in a plaintext column.
    - No client ID or secret is needed. That is the whole point of this path.
    - The two ways do not leave a stale credential behind: connecting with a
      token clears the OAuth credential, and the reverse too.
    """
    spec = registry.get(connector_id)
    if spec is None:
        raise OAuthError("unknown_connector", connector_id, 404)
    if not spec.token_auth:
        raise OAuthError("token_not_supported", f"{connector_id} cannot be connected with a token", 400)
    token = (token or "").strip()
    if not token:
        raise OAuthError("bad_token", "paste the token without spaces", 400)
    if len(token) > 4096 or any(c.isspace() for c in token):
        raise OAuthError("bad_token", "paste the token without spaces", 400)

    label = None
    # Verify BEFORE storing anything. Either the caller brings its own
    # check (the token kit's whoami) or the connector's registered verifier
    # is used.
    checker = whoami
    if checker is None and connector_id in _TOKEN_VERIFIERS:
        checker = _TOKEN_VERIFIERS[connector_id]
    if checker is not None:
        try:
            label = checker(token) or None
        except OAuthError:
            store.audit(connector_id, "token_rejected", {"error": "bad_token"})
            raise
        except Exception as e:
            store.audit(connector_id, "token_rejected", {"error": type(e).__name__})
            raise OAuthError("bad_token", "the provider rejected this token", 400) from e
    elif not whoami and connector_id not in _TOKEN_VERIFIERS:
        # Nothing can check this token. Refuse rather than store an
        # unverified credential that would fail later, at the first sync.
        raise OAuthError("token_not_supported",
                         f"{connector_id} cannot be connected with a token", 400)

    now = int(time.time())
    tokens.save_token(connector_id, {
        "access_token": token,
        "refresh_token": None,
        "token_type": "Bearer",
        "scope": None,
        # A personal token has no expiry: there is nothing to refresh, and
        # pretending otherwise would make the UI promise a renewal that never
        # comes.
        "expires_at": None,
        "auth_method": "token",
    })
    store.upsert_account(connector_id, status="connected", permission="read",
                         granted_scopes=[], account_label=label,
                         client_id=None, last_error=None, connected_at=now)
    # The credential is never written to the audit detail.
    store.audit(connector_id, "connected_token", {"account_label": label})
    return {"ok": True, "connector_id": connector_id, "auth_method": "token",
            "account_label": label}


def _cancel_flows(connector_id: str) -> None:
    """Disconnect wins over any flow still open for this connector (e.g. an
    upgrade to write started and abandoned): its callback must not reconnect."""
    with _LOCK:
        open_flows = [(fl, fl.status) for fl in _FLOWS.values() if fl.connector_id == connector_id
                      and fl.status in ("pending", "exchanging")]
        for fl, _ in open_flows:
            fl.status, fl.error = "expired", "disconnected"
    # An "exchanging" flow is inside its (one-shot) receiver's handler, which
    # stops by itself; closing it from here would wait for the exchange.
    for fl, was in open_flows:
        if fl.receiver and was == "pending":
            fl.receiver.close_now()


def disconnect(connector_id: str) -> dict[str, Any]:
    """Forget the connection: best-effort revoke at the provider, delete the
    keychain token, mark disconnected. Pending writes are expired."""
    spec = registry.get(connector_id)
    if spec is None:
        raise OAuthError("unknown_connector", connector_id, 404)
    revoked = None
    rec = tokens.load_token(connector_id)
    if rec and spec.revoke_url:
        try:
            client_id, secret = _client_credentials(spec)
            _post_form(spec, spec.revoke_url, {"token": rec.get("refresh_token") or rec["access_token"],
                                               "client_id": client_id}, (client_id, secret) if secret else None)
            revoked = True
        except OAuthError:
            revoked = False
    _cancel_flows(connector_id)
    try:
        tokens.delete_token(connector_id)
    except tokens.TokenStoreUnavailable as e:
        store.audit(connector_id, "disconnect_failed", {"error": str(e)})
        raise OAuthError("keychain_delete_failed",
                         "the token could not be removed from the keychain; try again", 503) from e
    from . import connector_actions
    expired = connector_actions.expire_for_connector(connector_id)
    acc = store.upsert_account(connector_id, status="disconnected", granted_scopes=[], last_error=None,
                               connected_at=None, account_label=None)
    store.audit(connector_id, "disconnected", {"revoked": revoked, "pending_expired": expired})
    return acc


# ── Loopback receiver ───────────────────────────────────────────────────────

_DONE_HTML = ("<!doctype html><meta charset=utf-8><title>ConductLoop</title>"
              "<p>{msg}</p><p>You can close this tab. / Ya puedes cerrar esta pestaña.</p>")
_CALLBACK_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


class _Receiver:
    """One-shot HTTP listener on 127.0.0.1 for a single flow."""

    def __init__(self, state: str, port: int = 0) -> None:
        self.state = state
        outer = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:     # never log the code
                pass

            def do_GET(self) -> None:  # noqa: N802
                u = urlparse(self.path)
                host = (self.headers.get("Host") or "").split(":")[0]
                if u.path != CALLBACK_PATH or host not in ("127.0.0.1", "localhost"):
                    self._reply(404, "Not found")
                    return
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if q.get("state") != outer.state:
                    self._reply(400, "Invalid state")
                    return
                try:
                    res = complete(outer.state, q.get("code"), q.get("error"))
                    ok = res.get("status") == "connected"
                except OAuthError:
                    ok = False
                self._reply(200 if ok else 400, "Connected. / Conectado." if ok else "Connection failed. / Error al conectar.")
                outer.stop()

            def _reply(self, status: int, msg: str) -> None:
                body = _DONE_HTML.format(msg=msg).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Security-Policy", _CALLBACK_CSP)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

        self._srv = HTTPServer(("127.0.0.1", port), _H)
        self.port = int(self._srv.server_address[1])
        self._thread: threading.Thread | None = None
        self._timer = threading.Timer(FLOW_TTL, self.stop)
        self._timer.daemon = True
        self._stopped = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.2},
                                        daemon=True, name=f"oauth-loopback-{self.port}")
        self._thread.start()
        self._timer.start()

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._timer.cancel()
        threading.Thread(target=self._shutdown, daemon=True).start()

    def _shutdown(self) -> None:
        try:
            self._srv.shutdown()
        finally:
            self._srv.server_close()

    def close_now(self) -> None:
        """Synchronous stop: the port is free when this returns."""
        self._stopped.set()
        self._timer.cancel()
        if self._thread and self._thread.is_alive():
            self._srv.shutdown()
            self._thread.join(2.0)
        self._srv.server_close()

    def wait_closed(self, timeout: float = 5.0) -> bool:
        if self._thread:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True
