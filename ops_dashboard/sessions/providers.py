"""Provider adapter for session runners (multi-agent PR 6, D1/D5).

A session always uses its OWN explicit provider + model; it never falls back
to another provider. ``resolve_for_session`` is the single place that turns a
session row into a ``brain.Provider``:

* With #15 merged, ``brain.resolve_provider(name, model=…, strict=True)``
  already refuses a provider in cooldown and pins the model; we call it as is.
* On a base without #15 (this branch), a minimal shim gives the same contract
  without duplicating #15: refuse if brain's cooldown is active, call the old
  ``resolve_provider(name)``, refuse if it returned a different provider, pin
  the model on the instance. When #15 lands the shim branch is dead code and
  can be deleted (the signature probe picks #15's path automatically).
* ``key_ref`` (D5) names a secrets_store item; its value is set on the
  provider instance only (never env/config/DB) and only for providers whose
  ``ask`` reads ``self.api_key`` at call time.

Also here: one global semaphore per LOCAL provider (Ollama runs one request
at a time across all sessions) and a small per-key_ref cooldown so a key
that just failed with auth/balance/rate errors isn't hammered by every session.
"""
from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from . import limits

LOCAL_PROVIDERS = ("ollama",)
FREE_PROVIDERS = ("ollama",)          # cost 0 (matches SessionStore.record_event)
_KEY_ATTR_PROVIDERS = ("anthropic", "openai", "minimax", "gateway")
COOLDOWN_SECONDS = {"invalid_key": 600, "insufficient_credit": 600, "rate_limited": 60}


class ProviderUnavailable(RuntimeError):
    def __init__(self, provider: str, reason: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.provider, self.reason, self.retry_after = provider, reason, retry_after


def _brain() -> Any:
    from .. import brain
    return brain


def resolve_for_session(session: dict) -> Any:
    brain = _brain()
    name, model = str(session["provider"]).lower(), session["model"]
    key_ref = session.get("key_ref")
    if key_ref and name not in _KEY_ATTR_PROVIDERS:
        raise ProviderUnavailable(name, "key_ref_unsupported",
                                  f"key_ref is not supported for provider {name!r}")
    # Rows written before the create-time check (or edited by hand) are
    # re-checked before anything else, so the secret is never read for them.
    if key_ref and limits.key_ref_provider(str(key_ref)) != name:
        raise ProviderUnavailable(name, "key_ref_mismatch",
                                  f"key_ref belongs to another provider, not {name!r}")
    params = inspect.signature(brain.resolve_provider).parameters
    if "strict" in params and "model" in params:                     # #15 behaviour
        try:
            p = brain.resolve_provider(name, model=model, strict=True)
        except brain.ProviderError as e:
            raise ProviderUnavailable(name, getattr(e, "reason", e.code), e.message,
                                      getattr(e, "retry_after", None)) from e
    else:                                                            # minimal shim
        if brain._provider_is_in_cooldown(name):
            raise ProviderUnavailable(name, "cooldown",
                                      f"provider {name!r} is cooling down after a failure; "
                                      "sessions never switch providers")
        p = brain.resolve_provider(name)
        if getattr(p, "name", None) != name:
            raise ProviderUnavailable(name, "mismatch", f"provider {name!r} could not be resolved")
        if model:
            p.model = model
    if key_ref:
        from .. import secrets_store
        key = secrets_store.get_secret(key_ref)
        if not key:
            raise ProviderUnavailable(name, "key_missing", f"no key stored under {key_ref!r}")
        p.api_key = key
    return p


ProviderFactory = Callable[[dict], Any]

# ── Ollama: one request at a time, process-wide ───────────────────────────

_LOCAL_SEMAPHORES: dict[str, threading.Semaphore] = {n: threading.Semaphore(1)
                                                     for n in LOCAL_PROVIDERS}


@contextmanager
def provider_slot(provider: str, cancel: threading.Event | None = None) -> Iterator[bool]:
    """Yields True once the caller may call ``provider``; False if cancelled
    while waiting. Cloud providers never wait."""
    sem = _LOCAL_SEMAPHORES.get(provider.lower())
    if sem is None:
        yield True
        return
    while not sem.acquire(timeout=0.2):
        if cancel is not None and cancel.is_set():
            yield False
            return
    try:
        yield True
    finally:
        sem.release()


# ── per-key_ref cooldown ──────────────────────────────────────────────────

_COOLDOWN: dict[str, float] = {}
_COOLDOWN_LOCK = threading.Lock()


def cooldown_key(session: dict) -> str:
    return session.get("key_ref") or f"{session['provider']}:default"


def mark_failure(session: dict, code: str, now: float | None = None) -> int | None:
    secs = COOLDOWN_SECONDS.get(code)
    if not secs:
        return None
    with _COOLDOWN_LOCK:
        _COOLDOWN[cooldown_key(session)] = (now or time.time()) + secs
    return secs


def cooldown_remaining(session: dict, now: float | None = None) -> int:
    with _COOLDOWN_LOCK:
        until = _COOLDOWN.get(cooldown_key(session), 0.0)
    return max(0, int(until - (now or time.time())))


def reset_cooldowns() -> None:
    with _COOLDOWN_LOCK:
        _COOLDOWN.clear()
