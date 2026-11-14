"""First run: the welcome page and the state it needs.

GET  /welcome                   the page (strict CSP: no inline code, nothing remote)
GET  /welcome.css
GET  /api/onboarding/state      {fresh, dismissed, completed, vault_source, ...}
POST /api/onboarding/dismiss    "not now": do not show the page again
POST /api/onboarding/complete   the user went through it

Local only, like the other settings routes. Nothing here ever carries a key:
the AI key is put in through ``/api/credentials/byok.<provider>.default`` (the
keychain or the encrypted vault) and its value is never read back.

``/welcome`` used to be an alias of ``/tour``; the quick tour stays on ``/tour``.
"""
from __future__ import annotations

from . import onboarding
from .routes_learning import PAGE_CSP, _body, _err, _local_only, _send_json, _serve_web
from .routing import router

router.add("GET", "/welcome", _serve_web("welcome.html", "text/html; charset=utf-8",
                                         {"Content-Security-Policy": PAGE_CSP}))
router.add("GET", "/welcome.css", _serve_web("welcome.css", "text/css; charset=utf-8"))


@router.get("/api/onboarding/state")
@_local_only
def onboarding_state(handler) -> None:
    _send_json(handler, 200, {"ok": True, **onboarding.state()})


def _mark(handler, kind: str) -> None:
    try:
        onboarding.mark(kind)
    except Exception as e:
        _err(handler, 500, "could_not_save", f"{type(e).__name__}")
        return
    _send_json(handler, 200, {"ok": True, **onboarding.state()})


@router.post("/api/onboarding/dismiss")
@_local_only
def onboarding_dismiss(handler) -> None:
    _mark(handler, "dismissed")


@router.post("/api/onboarding/complete")
@_local_only
def onboarding_complete(handler) -> None:
    _mark(handler, "completed")


@router.get("/api/onboarding/demo")
@_local_only
def onboarding_demo_state(handler) -> None:
    """Is the workspace showing sample data?"""
    _send_json(handler, 200, {"ok": True, **onboarding.demo_state()})


@router.post("/api/onboarding/demo")
@_local_only
def onboarding_demo_seed(handler) -> None:
    """Load the sample workspace, so a first-time user can see the product
    before they have a vault or an API key. Refused once there are real notes:
    the fixture must never end up mixed with the user's own material."""
    body = _body(handler) or {}
    lang = body.get("lang")
    if not isinstance(lang, str) or lang.lower() not in ("es", "en"):
        lang = "en"
    try:
        result = onboarding.seed_demo(lang=lang)
    except Exception as e:
        _err(handler, 500, "could_not_seed", f"{type(e).__name__}")
        return
    if not result.get("ok"):
        _err(handler, 409, str(result.get("error") or "could_not_seed"),
             "the workspace already holds your own notes")
        return
    _send_json(handler, 200, result)


@router.post("/api/onboarding/demo/clear")
@_local_only
def onboarding_demo_clear(handler) -> None:
    """Remove the sample data. Only ever touches rows tagged as sample data."""
    try:
        result = onboarding.clear_demo()
    except Exception as e:
        _err(handler, 500, "could_not_clear", f"{type(e).__name__}")
        return
    _send_json(handler, 200, result)
