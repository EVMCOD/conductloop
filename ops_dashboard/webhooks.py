"""Outbound webhooks for dashboard events.

Supports Slack-compatible and Discord-compatible incoming webhooks
(JSON POST with text field).

Configure via env vars (or config.json):
  SLACK_WEBHOOK_URL
  DISCORD_WEBHOOK_URL

Telegram is not a notification target any more (removed 2026-11-06): the
Telegram channel (Channels page) is the only way ConductLoop talks to
Telegram, and it sends only run results to paired chats.

Use:
  from ops_dashboard import webhooks
  webhooks.notify("Acme: URL acme.example failed (500)", severity="warn")
"""
from __future__ import annotations

import json
import os
from typing import Literal

import httpx

from . import app_paths as _app_paths

CONFIG_PATH = _app_paths.config_path()


def _load_cfg() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _get(name: str) -> str | None:
    return os.environ.get(name) or _load_cfg().get(name)


def _post_json(url: str, payload: dict, timeout: float = 5.0) -> bool:
    try:
        r = httpx.post(url, json=payload, timeout=timeout)
        return 200 <= r.status_code < 300
    except Exception:
        return False


def _send_slack(text: str) -> bool:
    url = _get("SLACK_WEBHOOK_URL")
    if not url:
        return False
    return _post_json(url, {"text": text})


def _send_discord(text: str) -> bool:
    url = _get("DISCORD_WEBHOOK_URL")
    if not url:
        return False
    return _post_json(url, {"content": text})


Severity = Literal["info", "warn", "alert"]


def notify(text: str, *, severity: Severity = "info") -> dict:
    """Send a notification to all configured channels. Returns per-channel results."""
    icon = {"info": "ℹ️", "warn": "⚠️", "alert": "🚨"}.get(severity, "·")
    msg = f"{icon} ops-dashboard · {text}"
    results = {
        "slack":    _send_slack(msg),
        "discord":  _send_discord(msg),
    }
    return results


def status() -> dict:
    return {
        "slack": bool(_get("SLACK_WEBHOOK_URL")),
        "discord": bool(_get("DISCORD_WEBHOOK_URL")),
    }


# --- Specific event helpers ---

def url_failed(name: str, status_code: int | None) -> dict:
    return notify(f"URL {name} is now failing ({status_code or 'no response'})", severity="warn")


def url_recovered(name: str) -> dict:
    return notify(f"URL {name} recovered", severity="info")


def new_activity(project: str, summary: str) -> dict:
    return notify(f"{project}: {summary}", severity="info")
