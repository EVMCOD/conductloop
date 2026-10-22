"""Offline fakes for the native connectors (Google, Slack, Notion, Microsoft).

``ProviderFake`` is an ``httpx.MockTransport`` that plays a real provider:
* the token endpoint checks PKCE S256, redirect_uri, client_id, single-use
  codes and the client secret (HTTP Basic) exactly like the provider documents;
* API routes answer with **recorded responses** from
  ``tests/fixtures/connectors/<name>.json``;
* any other host raises ``NetworkForbidden`` — a test can never go online.

``connect()`` drives the framework's real OAuth flow (``listen=False``) through
the fake consent screen, returning the parsed authorize-URL parameters so tests
can pin exactly which scopes a connector asks for.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "connectors"


class NetworkForbidden(AssertionError):
    pass


def recorded(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


Handler = Callable[[httpx.Request], httpx.Response]


class ProviderFake:
    def __init__(self, *, token_url: str, hosts: tuple[str, ...], routes: dict[tuple[str, str], Any],
                 client_secret: str | None = None, token_json_body: bool = False,
                 token_response: dict[str, Any] | None = None, scope_separator: str = " ") -> None:
        self.token_url = urlparse(token_url)
        self.hosts = set(hosts)
        self.routes = routes                    # (METHOD, "host/path") -> dict | Handler
        self.client_secret = client_secret
        self.token_json_body = token_json_body
        self.scope_separator = scope_separator
        self.token_response = token_response or {"access_token": "fake-at-" + secrets.token_hex(4),
                                                 "refresh_token": "fake-rt", "expires_in": 3599,
                                                 "token_type": "Bearer"}
        self.codes: dict[str, dict[str, Any]] = {}
        self.issued: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.writes: list[dict[str, Any]] = []

    # consent screen ------------------------------------------------------
    def authorize(self, authorize_url: str, *, grant: list[str] | None = None) -> dict[str, str]:
        q = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        assert q["response_type"] == "code"
        code = secrets.token_urlsafe(10)
        asked = [s for s in q.get("scope", "").split(self.scope_separator) if s]
        self.codes[code] = {"challenge": q.get("code_challenge"), "redirect_uri": q["redirect_uri"],
                            "client_id": q["client_id"], "scopes": asked if grant is None else grant}
        return {"state": q["state"], "code": code}

    # transport -----------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host not in self.hosts:
            raise NetworkForbidden(f"test tried to reach {request.url}")
        if request.url.host == self.token_url.hostname and request.url.path == self.token_url.path:
            return self._token(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.issued:
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})
        raw_path = request.url.raw_path.split(b"?", 1)[0].decode()      # still percent-encoded
        key = (request.method, f"{request.url.host}{raw_path}")
        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404, json={"error": {"status": "NOT_FOUND", "path": key[1]}})
        if callable(route):
            return route(request)
        return httpx.Response(200, json=route)

    def _form(self, request: httpx.Request) -> dict[str, str]:
        if self.token_json_body:
            assert request.headers.get("content-type", "").startswith("application/json")
            return {k: str(v) for k, v in json.loads(request.content or b"{}").items()}
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = self._form(request)
        if self.client_secret is not None:
            cid = form.get("client_id") or next(iter(self.codes.values()), {}).get("client_id", "")
            basic = request.headers.get("Authorization", "")
            ok = False
            if basic.startswith("Basic "):
                user, _, pw = base64.b64decode(basic[6:]).decode().partition(":")
                ok = pw == self.client_secret and (not cid or user == cid)
            if not ok:
                return httpx.Response(401, json={"error": "invalid_client"})
        elif "Authorization" in request.headers:
            return httpx.Response(400, json={"error": "public_client_must_not_authenticate"})
        if form.get("grant_type") == "refresh_token":
            body = dict(self.token_response, access_token="fake-at-refreshed")
            self.issued.add(body["access_token"])
            return httpx.Response(200, json=body)
        info = self.codes.pop(form.get("code", ""), None)
        if info is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if info["challenge"] is not None:
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != info["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "why": "pkce"})
        if form.get("redirect_uri") != info["redirect_uri"]:
            return httpx.Response(400, json={"error": "invalid_grant", "why": "redirect_uri"})
        body = dict(self.token_response)
        body["scope"] = self.scope_separator.join(info["scopes"])
        if "access_token" in body:            # e.g. Slack {"ok": false, "error": …} has none
            self.issued.add(body["access_token"])
        return httpx.Response(200, json=body)


def connect(spec_id: str, fake: ProviderFake, *, permission: str = "read", client_id: str = "byok-client",
            client_secret: str | None = None, grant: list[str] | None = None) -> dict[str, str]:
    """Run the real framework flow against the fake. Returns the authorize params."""
    from ops_dashboard import connector_oauth as oauth
    from ops_dashboard import connector_store as store
    from ops_dashboard import connector_tokens as tokens

    store.upsert_account(spec_id, client_id=client_id)
    if client_secret:
        tokens.save_client_secret(spec_id, client_secret)
    flow = oauth.start(spec_id, permission, listen=False)
    params = {k: v[0] for k, v in parse_qs(urlparse(flow["authorize_url"]).query).items()}
    params["_authorize_base"] = flow["authorize_url"].split("?", 1)[0]
    cb = fake.authorize(flow["authorize_url"], grant=grant)
    res = oauth.complete(cb["state"], cb["code"])
    assert res["status"] == "connected", res
    return params


def sqlite_text(db_path: Any) -> str:
    """Every value of every ordinary table as text (iterdump trips over the
    documents_fts external-content table on main — #20)."""
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                           "AND name NOT LIKE '%fts%' AND name NOT LIKE 'sqlite_%'")]
        parts = []
        for n in names:
            parts += [repr(row) for row in con.execute(f'SELECT * FROM "{n}"')]  # noqa: S608
        return "\n".join(parts)
    finally:
        con.close()
