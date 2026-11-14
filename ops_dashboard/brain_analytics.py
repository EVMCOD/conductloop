"""Self-observability for the Brain.

Rolls up what ConductLoop already logs — LLM usage (usage_log), spend (costs),
memory size, decisions, and inbox — into one snapshot so you can see how the
second brain is actually being used and what it costs.

    GET /api/brain/analytics?days=30
"""
from __future__ import annotations

import logging

from . import db

log = logging.getLogger("conductloop.brain_analytics")


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        log.debug("analytics section failed", exc_info=True)
        return default


def analytics(days: int = 30) -> dict:
    usage = _safe(lambda: db.usage_summary(days=days), {})
    costs = _safe(db.cost_summary, {})
    memory_count = _safe(lambda: len(db.memory_list()), 0)
    decisions = _safe(lambda: len(db.list_decisions(limit=100000)), 0)

    def _captures():
        from . import capture
        return capture.stats()

    return {
        "days": days,
        "llm": {
            "calls": usage.get("calls", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "by_provider": usage.get("by_provider", []),
        },
        "spend": {
            "total_cents": costs.get("total_cents", 0),
            "by_provider": costs.get("by_provider", []),
        },
        "memory_entries": memory_count,
        "decisions": decisions,
        "inbox": _safe(_captures, {"open": 0, "total": 0}),
    }
