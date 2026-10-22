"""Local learning signals (design doc 2026-10-21-conductloop-learning-design.md, PR L3).

* 👍/👎 feedback: always stored locally when the user clicks (D7).
* Failure log: OFF by default, error codes only, 30-day retention (D6).

Nothing here sends data anywhere. ``export_local`` writes a JSON file to the
local data dir that the user may choose to share by hand.
"""
from __future__ import annotations

from .store import (
    FAILURE_KINDS,
    FEEDBACK_REASONS,
    FEEDBACK_TARGETS,
    RETENTION_DAYS,
    LearningError,
    clear_failures,
    delete_feedback,
    export_local,
    failure_log_enabled,
    list_failures,
    list_feedback,
    purge_expired,
    record_failure,
    record_feedback,
    resolve_brain_message,
    set_failure_log_enabled,
    summary,
)

__all__ = [
    "FAILURE_KINDS", "FEEDBACK_REASONS", "FEEDBACK_TARGETS", "RETENTION_DAYS", "LearningError",
    "clear_failures", "delete_feedback", "export_local", "failure_log_enabled", "list_failures",
    "list_feedback", "purge_expired", "record_failure", "record_feedback", "resolve_brain_message",
    "set_failure_log_enabled", "summary",
]
