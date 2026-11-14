"""Google access tokens over plain HTTPS, without Google's client libraries.

Google Analytics, Search Console and Google Play were read through
``google-analytics-data`` and ``google-auth``. Those are optional
dependencies, and the packaged desktop app does not ship them, so in the
app every poll failed with "No module named 'google.analytics'" (or
'google.auth'): the data stopped on the day people moved to the app, and the
anomaly check kept comparing that last, half-synced day with the week
before it, as a 71% drop, for weeks.

Both kinds of stored credential are a single HTTPS call away from an access
token, with what the app already ships (httpx, cryptography):

* an OAuth *authorized user* blob (``refresh_token`` + ``client_id`` +
  ``client_secret``), the one the connect flow saves: a refresh-token grant;
* a *service account* key (``client_email`` + ``private_key``): a JWT signed
  with RS256 and exchanged with the jwt-bearer grant.

Tokens are cached in memory until a minute before they expire, so polling
seven sites refreshes once, not seven times.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from typing import Any

import httpx

TOKEN_URI = "https://oauth2.googleapis.com/token"
_JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

_cache: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


class GoogleAuthError(RuntimeError):
    """The credential could not be turned into an access token."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _cache_key(info: dict[str, Any], scopes: list[str]) -> str:
    who = info.get("refresh_token") or info.get("client_email") or ""
    raw = json.dumps([info.get("client_id") or "", who, sorted(scopes)])
    return hashlib.sha256(raw.encode()).hexdigest()


def _service_account_assertion(info: dict[str, Any], scopes: list[str], token_uri: str,
                               now: int) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    header = {"alg": "RS256", "typ": "JWT"}
    if info.get("private_key_id"):
        header["kid"] = info["private_key_id"]
    claims = {
        "iss": info["client_email"],
        "scope": " ".join(scopes),
        "aud": token_uri,
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "."
                     + _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64url(signature)


def _explain(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    code = body.get("error") or f"HTTP {resp.status_code}"
    desc = body.get("error_description") or ""
    if code == "invalid_grant":
        # Revoked, expired (an OAuth app in Testing mode drops refresh tokens
        # after 7 days) or the password changed: only a new sign-in fixes it.
        return ("Google rejected the stored sign-in (invalid_grant): connect the "
                "Google account again" + (f" — {desc}" if desc else ""))
    return f"{code}" + (f": {desc}" if desc else "")


def access_token(credentials: dict[str, Any] | str, scopes: list[str], *,
                 client: httpx.Client | None = None) -> str:
    """An access token for ``credentials`` (dict or JSON string)."""
    info = json.loads(credentials) if isinstance(credentials, str) else dict(credentials or {})
    key = _cache_key(info, scopes)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[1] > now:
            return hit[0]

    token_uri = info.get("token_uri") or TOKEN_URI
    if info.get("refresh_token"):
        missing = [k for k in ("client_id", "client_secret") if not info.get(k)]
        if missing:
            raise GoogleAuthError(f"the stored Google sign-in has no {', '.join(missing)}")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": info["refresh_token"],
            "client_id": info["client_id"],
            "client_secret": info["client_secret"],
        }
    elif info.get("client_email") and info.get("private_key"):
        data = {"grant_type": _JWT_GRANT,
                "assertion": _service_account_assertion(info, scopes, token_uri, int(now))}
    else:
        raise GoogleAuthError("the stored credential is neither a Google sign-in "
                              "(refresh_token) nor a service account key (private_key)")

    http = client or httpx
    try:
        resp = http.post(token_uri, data=data, timeout=20)
    except httpx.HTTPError as e:
        raise GoogleAuthError(f"could not reach Google: {type(e).__name__}") from None
    if resp.status_code != 200:
        raise GoogleAuthError(_explain(resp))
    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise GoogleAuthError("Google answered without an access token")
    ttl = float(body.get("expires_in") or 3600)
    with _lock:
        _cache[key] = (token, now + max(ttl - 60, 0))
    return token


def clear_cache() -> None:
    with _lock:
        _cache.clear()
