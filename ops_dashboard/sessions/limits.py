"""Approved limits (design doc D4/D5) and input validation for sessions."""
from __future__ import annotations

import os
import re

DEFAULT_MAX_ACTIVE_SESSIONS = 3   # D4: 3 concurrent sessions by default…
HARD_MAX_ACTIVE_SESSIONS = 8      # …configurable up to a hard maximum of 8.
DEFAULT_MAX_ITERATIONS = 60       # D4: 60 iterations per session by default…
# …and, unless configured, also the largest value a session may ask for. The
# limit lives in code (not in the schema CHECK) so raising it needs no
# migration: pass ``max_iterations_limit`` to SessionStore or set the env var.
MAX_ITERATIONS_ENV = "CONDUCTLOOP_SESSIONS_MAX_ITERATIONS"
BUDGET_CURRENCY = "EUR"           # D4: budget is mandatory and in EUR.

SESSION_STATUSES = ("created", "provisioning", "idle", "running", "waiting_human",
                    "paused", "stalled", "closing", "closed", "failed")
# Sessions that hold a concurrency slot (everything not terminal).
ACTIVE_SESSION_STATUSES = tuple(s for s in SESSION_STATUSES if s not in ("closed", "failed"))
# Sessions allowed to pick up work.
CLAIMING_SESSION_STATUSES = ("created", "provisioning", "idle", "running")
TASK_STATUSES = ("queued", "claimed", "running", "needs_review", "done", "blocked",
                 "failed", "cancelled")
IN_FLIGHT_TASK_STATUSES = ("claimed", "running", "needs_review")
MESSAGE_KINDS = ("info", "request", "handoff", "conflict", "system")

# D5: sessions store a secrets_store item NAME, never a key value. Same shape as
# byok_keys.default_ref() ("byok.<provider>.<label>").
_KEY_REF_RE = re.compile(r"^byok\.[a-z0-9_-]{1,32}\.[A-Za-z0-9_-]{1,48}$")
# Shapes of real provider secrets; used to reject them as key_ref and to redact
# them from free text (events, messages) before anything is persisted.
SECRET_PATTERNS = re.compile(
    r"(sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"      # Anthropic / OpenAI
    r"|AIza[0-9A-Za-z_\-]{30,}"                     # Google
    r"|gh[pousr]_[A-Za-z0-9]{30,}"                  # GitHub tokens
    r"|xox[abpr]-[A-Za-z0-9-]{10,}"                 # Slack
    r"|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,})"  # JWT
)
REDACTED = "[redacted-secret]"


class SessionValidationError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field


class SessionLimitError(RuntimeError):
    """Concurrency limit reached (D4)."""


def key_ref_provider(key_ref: str) -> str:
    """The <provider> segment of a ``byok.<provider>.<label>`` reference."""
    return key_ref.split(".")[1]


def validate_key_ref(key_ref: str | None, provider: str | None = None) -> str | None:
    if key_ref is None:
        return None
    if not isinstance(key_ref, str) or SECRET_PATTERNS.search(key_ref) or not _KEY_REF_RE.match(key_ref):
        # Never echo the value back: it might be a real key pasted by mistake.
        raise SessionValidationError(
            "key_ref", "must be a keychain reference like 'byok.<provider>.<label>', "
                       "never a key value")
    # A key stored for one provider must never be sent to another one (e.g. an
    # OpenAI key to Anthropic, or to a user-configured gateway URL).
    if provider is not None and key_ref_provider(key_ref) != provider.strip().lower():
        raise SessionValidationError(
            "key_ref", f"belongs to provider {key_ref_provider(key_ref)!r}, "
                       f"not {provider.strip().lower()!r}")
    return key_ref


def resolve_max_active(max_active: int | None) -> int:
    if max_active is None:
        return DEFAULT_MAX_ACTIVE_SESSIONS
    if not isinstance(max_active, int) or not 1 <= max_active <= HARD_MAX_ACTIVE_SESSIONS:
        raise SessionValidationError(
            "max_active", f"must be between 1 and {HARD_MAX_ACTIVE_SESSIONS}")
    return max_active


def validate_budget(budget_cost_cents: object, currency: str, budget_tokens: object) -> None:
    if currency != BUDGET_CURRENCY:
        raise SessionValidationError("budget_currency", "budget must be in EUR")
    if not isinstance(budget_cost_cents, int) or isinstance(budget_cost_cents, bool) \
            or budget_cost_cents <= 0:
        raise SessionValidationError("budget_cost_cents",
                                     "a positive budget in EUR cents is mandatory")
    if budget_tokens is not None and (not isinstance(budget_tokens, int)
                                      or isinstance(budget_tokens, bool) or budget_tokens <= 0):
        raise SessionValidationError("budget_tokens", "must be a positive integer or None")


def resolve_max_iterations_limit(limit: int | None = None) -> int:
    """Upper bound for ``max_iterations``: explicit value, else the env var,
    else DEFAULT_MAX_ITERATIONS."""
    if limit is None:
        raw = os.environ.get(MAX_ITERATIONS_ENV, "").strip()
        if not raw:
            return DEFAULT_MAX_ITERATIONS
        try:
            limit = int(raw)
        except ValueError:
            raise SessionValidationError("max_iterations_limit",
                                         f"{MAX_ITERATIONS_ENV} must be an integer") from None
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise SessionValidationError("max_iterations_limit", "must be a positive integer")
    return limit


def validate_iterations(max_iterations: object, limit: int | None = None) -> int:
    cap = resolve_max_iterations_limit(limit)
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) \
            or not 1 <= max_iterations <= cap:
        raise SessionValidationError("max_iterations", f"must be between 1 and {cap}")
    return max_iterations


def redact(text: str) -> str:
    return SECRET_PATTERNS.sub(REDACTED, text)
