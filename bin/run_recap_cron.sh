#!/bin/bash
# Daily 08:00 Boardroom recap — regenerate Holding CEO synthesis + broadcast
# to the configured Discord channel.
#
# Run by LaunchAgent: ~/Library/LaunchAgents/com.opsdashboard.boardroom.recap.plist
# Runs the same flow as the Web UI's "↻ Regenerate" button: forces a fresh
# synthesis, then pushes it to Discord if DISCORD_WEBHOOK_URL is set.
# Telegram is not used here: the Telegram channel sends only run results.
set -e

OPS_HOME="$HOME/ops-dashboard"
LOG_DIR="$OPS_HOME/logs"
mkdir -p "$LOG_DIR"

cd "$OPS_HOME"
# shellcheck disable=SC1091
source .venv/bin/activate

python - <<'PY'
import time
from ops_dashboard import boardroom as br
from ops_dashboard import discord_bridge as dc

# 1) Force cache miss + regenerate synthesis
syn = br.recap_synthesis(br._recap_payload(), force=True)
src = syn.get("source", "?")
provider = syn.get("provider") or "(static fallback)"
text = (syn.get("text") or "(no synthesis)").strip()
title = f"☀️ Daily Boardroom Recap ({src}, {provider}) — {time.strftime('%Y-%m-%d')}"
print(f"[cron] synthesis: source={src} provider={provider} chars={len(text)}")

# 2) Discord outbound
try:
    if dc.is_configured():
        out = dc.send_message(f"**{title}**\n\n{text}")
        print(f"[cron] discord: ok={out.get('ok')}")
    else:
        print("[cron] discord: DISCORD_WEBHOOK_URL not set, skipping")
except Exception as e:
    print(f"[cron] discord error: {e!r}")

PY

echo "[cron] done at $(date '+%Y-%m-%d %H:%M:%S')"
