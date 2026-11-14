"""The language the person chose.

GET  /api/locale    {"locale": "es", "supported": ["en", "es"], "labels": {...}}
POST /api/locale    {"locale": "es"} — remember it (this computer only)

The choice is the person's, so it is a normal setting like any other: it is
stored in the app's configuration and read back by the server, so everything
the server writes (the agent roles, the answer when there is no key yet) is
in the same language as the page around it.
"""
from __future__ import annotations

from . import i18n
from .routes_learning import _body, _err, _send_json
from .routing import router


def _payload() -> dict:
    locale = i18n.current_locale()
    return {
        "ok": True,
        "locale": locale,
        # Whether this language was *chosen* or is only the starting point.
        # A browser that only guessed must not override a real choice.
        "chosen": i18n.chosen(),
        "supported": list(i18n.SUPPORTED),
        "labels": {code: i18n.LABELS[code] for code in i18n.SUPPORTED},
    }


@router.get("/api/locale")
def locale_get(handler) -> None:
    _send_json(handler, 200, _payload())


@router.post("/api/locale")
def locale_set(handler) -> None:
    data = _body(handler)
    if data is None:
        return
    wanted = data.get("locale")
    if not isinstance(wanted, str) or not wanted.strip():
        _err(handler, 400, "bad_request", "locale is required")
        return
    if not i18n.is_supported(wanted):
        _err(handler, 400, "unsupported_locale",
             f"supported languages: {', '.join(i18n.SUPPORTED)}")
        return
    i18n.set_locale(wanted)
    _send_json(handler, 200, _payload())
