"""Shared bits for the Google connectors (Calendar, Gmail, Drive).

BYOK (decision C1): the user creates a "Desktop app" OAuth client in their own
Google Cloud project (see the guide on /connectors) and pastes its client ID
and client secret. Google issues a secret to every Desktop client but documents
it as non-confidential; we still keep it in the keychain only.

* Redirect: the framework's loopback ``http://127.0.0.1:<free port>/callback``
  (Desktop clients accept any loopback port; nothing to register).
* Each Google product is a separate connector with its own consent and token,
  so Calendar never holds Gmail scopes and vice versa (least privilege). They
  can share **one** BYOK app: paste the client ID/secret once and use
  "Usar la misma app de Google" (``share_client``), which copies only the
  client credentials, never a token.
* ``revoke_url`` is deliberately empty: Google's revoke "removes all OAuth 2.0
  scopes previously granted to a project, invalidating any issued access or
  refresh tokens for all clients registered under that project", so revoking
  Calendar would also kill Gmail/Drive when they share a project. Disconnect
  deletes the token from the keychain; the user can revoke the whole grant at
  https://myaccount.google.com/permissions.
"""
from __future__ import annotations

from typing import Any

import httpx

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_HOSTS = ("accounts.google.com", "oauth2.googleapis.com")
DOCS_URL = "https://developers.google.com/identity/protocols/oauth2/native-app"
MAX_PAGES = 10


def scope(name: str) -> str:
    return f"https://www.googleapis.com/auth/{name}"


def oauth_kwargs() -> dict[str, Any]:
    """ConnectorSpec kwargs shared by every Google connector."""
    return {
        "auth": "oauth_pkce",
        "authorize_url": AUTHORIZE_URL,
        "token_url": TOKEN_URL,
        "revoke_url": "",
        "client_secret_required": True,
        # Disconnect keeps the upstream grant (see revoke_url above) and Google
        # only issues a refresh token on the first consent unless asked again:
        # without these a reconnect would have no refresh token and break once
        # the 1-hour access token expires.
        "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        "docs_url": DOCS_URL,
    }


def join_group(connector_id: str) -> None:
    """All Google connectors can share one BYOK "Desktop app" client (one
    project, one client ID/secret) while keeping separate tokens and scopes."""
    from . import join_client_group
    join_client_group("google", connector_id)


class GoogleAPIError(RuntimeError):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"google_api_{status}:{reason}")
        self.status = status
        self.reason = reason


def _raise_for(r: httpx.Response) -> None:
    if r.status_code >= 400:
        reason = ""
        try:
            err = r.json().get("error")
            reason = (err.get("status") or err.get("message") or "") if isinstance(err, dict) else str(err or "")
        except (ValueError, AttributeError):
            pass
        raise GoogleAPIError(r.status_code, reason[:80])


def get_json(client: Any, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    r: httpx.Response = client.get(url, params=params or {})
    _raise_for(r)
    body = r.json()
    if not isinstance(body, dict):
        raise GoogleAPIError(502, "not_an_object")
    return body


def send_json(client: Any, method: str, url: str, *, params: dict[str, Any] | None = None,
              body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write call (POST/PATCH/DELETE). Only ever reached from a confirmed
    pending action (connector_actions.confirm)."""
    kw: dict[str, Any] = {"params": params or {}}
    if body is not None:
        kw["json"] = body
    r: httpx.Response = client.request(method, url, **kw)
    _raise_for(r)
    if not r.content:
        return {}
    out = r.json()
    return out if isinstance(out, dict) else {}


def paged(client: Any, url: str, params: dict[str, Any], *, key: str = "items",
          max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
    return paged_ex(client, url, params, key=key, max_pages=max_pages)[0]


def paged_ex(client: Any, url: str, params: dict[str, Any], *, key: str = "items",
             max_pages: int = MAX_PAGES) -> tuple[list[dict[str, Any]], bool]:
    """Like paged(), plus whether the listing is complete. A nextPageToken left
    after ``max_pages`` means a partial result: callers must not prune from it."""
    out: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(max_pages):
        p = dict(params)
        if token:
            p["pageToken"] = token
        body = get_json(client, url, p)
        out.extend(x for x in body.get(key) or [] if isinstance(x, dict))
        token = body.get("nextPageToken")
        if not token:
            return out, True
    return out, False
