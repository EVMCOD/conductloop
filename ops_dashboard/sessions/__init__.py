"""Multi-agent sessions — data layer, worktrees, code tools (multi-agent PRs 3–6).

``store.SessionStore`` is the repository over the tables created by migration 8;
``limits`` holds the approved defaults (D4) and validation; ``paths`` the pure
claim-overlap / hot-file rules (D3); ``worktrees`` the per-session git worktrees
(PR 4); ``code_tools`` the agent's file/search/diff/run tools confined to the
session worktree and ``sandbox`` the whitelisted, OS-sandboxed runner (PR 5).
``providers`` / ``runner`` / ``supervisor`` run the agent loop per session with
its explicit BYOK provider (PR 6). No API or UI here — those land in later PRs.
"""
from __future__ import annotations

from .limits import (
    DEFAULT_MAX_ACTIVE_SESSIONS,
    DEFAULT_MAX_ITERATIONS,
    HARD_MAX_ACTIVE_SESSIONS,
    SessionLimitError,
    SessionValidationError,
)
from .paths import PathConflictError
from .store import SessionStore

__all__ = [
    "DEFAULT_MAX_ACTIVE_SESSIONS", "DEFAULT_MAX_ITERATIONS", "HARD_MAX_ACTIVE_SESSIONS",
    "PathConflictError", "SessionLimitError", "SessionStore", "SessionValidationError",
]
