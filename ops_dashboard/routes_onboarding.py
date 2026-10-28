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
from .routes_learning import PAGE_CSP, _err, _local_only, _send_json, _serve_web
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
