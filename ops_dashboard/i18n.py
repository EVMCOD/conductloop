"""The language the app speaks.

Every person chooses their own language — that is the point: the app is sold
to people who do not read English the same way, and the desktop app runs on
*their* computer, so the language is their decision and not ours.

The choice lives in two places on purpose:

* **the app's configuration** (``config.json``), so the server speaks it too:
  the agent roles, the "no key yet" answer, everything the server writes.
* **the browser's ``localStorage``**, so the page paints in that language
  straight away, before the server answers anything.

Until somebody chooses, nobody has chosen: the browser's own language is the
starting point (a Spanish browser on a fresh install starts in Spanish, and
the request the server is answering is in that language too), and the first
explicit choice wins forever after. The server tells the browser whether the
language was *chosen* (:func:`chosen`), so a browser that only guessed never
overrides a choice somebody else made.

Nothing here ever sends anything anywhere: the language stays on this
computer.
"""
from __future__ import annotations

from contextvars import ContextVar

SUPPORTED: tuple[str, ...] = ("en", "es")
DEFAULT = "en"

# The name each language calls itself in that language.
LABELS: dict[str, str] = {
    "en": "English",
    "es": "Español",
}

CONFIG_KEY = "locale"

# The language of the request being served, for the case where nobody has
# chosen one yet. Browsers send it in Accept-Language, so the server speaks
# the same language as the page it is answering without anybody having to
# choose anything first. It lives in a ContextVar because the server is
# threaded: each request is served in its own thread.
_request_language: ContextVar[str | None] = ContextVar("ops_request_language", default=None)


def normalize(locale: str | None) -> str:
    """A supported locale code, or the default."""
    code = (locale or "").strip().lower().replace("_", "-")
    if code in SUPPORTED:
        return code
    # "es-ES", "es_ES", "es-MX" -> "es"
    head = code.split("-")[0]
    return head if head in SUPPORTED else DEFAULT


def from_accept_language(header: str | None) -> str | None:
    """The first language of an ``Accept-Language`` header that we speak."""
    if not header:
        return None
    for part in header.split(","):
        tag = part.split(";")[0].strip().lower().replace("_", "-")
        if not tag:
            continue
        head = tag.split("-")[0]
        if head in SUPPORTED:
            return head
    return None


def set_request_language(header: str | None) -> None:
    """Remember the language of the request being served (this thread only)."""
    _request_language.set(from_accept_language(header))


def stored_locale() -> str | None:
    """The language the person chose, or None when nobody has chosen yet."""
    from . import brain

    try:
        cfg = brain._load_config()
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    raw = cfg.get(CONFIG_KEY)
    if isinstance(raw, str) and is_supported(raw):
        return normalize(raw)
    return None


def chosen() -> bool:
    """Has the person chosen a language? (False on a fresh install.)"""
    return stored_locale() is not None


def current_locale() -> str:
    """The language the app is in right now."""
    code = stored_locale()
    if code:
        return code
    # Nobody has chosen yet: the request's own language is the starting
    # point, and the default when it is not a language we speak.
    return _request_language.get() or DEFAULT


def set_locale(locale: str | None) -> str:
    """Remember the language the person chose. Returns the locale in use."""
    from . import brain

    code = normalize(locale)
    try:
        cfg = brain._load_config()
        if not isinstance(cfg, dict):
            cfg = {}
        if cfg.get(CONFIG_KEY) != code:
            cfg[CONFIG_KEY] = code
            brain._save_config(cfg)
    except Exception:
        pass
    return code


def label(locale: str | None = None) -> str:
    """The name of a language, in that language."""
    code = normalize(locale) if locale else current_locale()
    return LABELS.get(code, LABELS[DEFAULT])


def is_supported(locale: str | None) -> bool:
    code = (locale or "").strip().lower().replace("_", "-")
    if code in SUPPORTED:
        return True
    return code.split("-")[0] in SUPPORTED
