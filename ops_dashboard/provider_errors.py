"""Shared wire format for ``brain.ProviderUnavailableError`` (HTTP 503).

Every endpoint that calls the brain maps the error through these helpers so
the UI (``web/provider-errors.js``) always receives the same JSON:

    {"ok": false, "error": "provider_unavailable", "code": "provider_unavailable",
     "provider": "<name>", "model": "<model>|null", "retry_after": <seconds>,
     "reason": "cooldown", "message": "<english, safe to show>"}

No secrets: only the provider name, the requested model and timing.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .brain import ProviderUnavailableError

__all__ = ["ProviderUnavailableError", "payload", "send", "send_result", "sse_event",
           "run_error_text", "parse_run_error", "http_status_for"]

# Prefix used when the error has to be stored as text (task runs, agent logs).
RUN_ERROR_PREFIX = "provider_unavailable:"
_RUN_ERROR_RE = re.compile(r"^provider_unavailable:([A-Za-z0-9_.-]+)")


def payload(err: ProviderUnavailableError) -> dict:
    return err.payload()


def send(handler: Any, err: ProviderUnavailableError) -> None:
    """Write the 503 JSON response on a DashboardHandler-like object."""
    headers = {"Retry-After": str(err.retry_after)} if err.retry_after else None
    handler._send(503, json.dumps(err.payload()).encode(), "application/json", headers)


def sse_event(err: ProviderUnavailableError) -> bytes:
    """SSE ``data:`` frame for streams that already sent their headers."""
    return b"data: " + json.dumps({"type": "error", **err.payload()}).encode() + b"\n\n"


def run_error_text(err: ProviderUnavailableError) -> str:
    """Stable text stored in task_runs.error / agent logs."""
    return f"{RUN_ERROR_PREFIX}{err.provider} — {err.message}"


def parse_run_error(text: str | None) -> str | None:
    """Provider name if ``text`` was produced by :func:`run_error_text`."""
    m = _RUN_ERROR_RE.match(text or "")
    return m.group(1) if m else None


def http_status_for(result: dict, default: int) -> int:
    """503 when a run/wake result carries the provider_unavailable payload."""
    return 503 if (result or {}).get("error") == "provider_unavailable" else default


def send_result(handler: Any, result: dict, default_status: int, body: bytes) -> None:
    """Send an already-serialised result dict; 503 + Retry-After when it is a
    provider_unavailable payload, ``default_status`` otherwise."""
    status = http_status_for(result, default_status)
    retry = (result or {}).get("retry_after") if status == 503 else None
    handler._send(status, body, "application/json", {"Retry-After": str(retry)} if retry else None)
