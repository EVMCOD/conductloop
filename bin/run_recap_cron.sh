#!/bin/bash
# Daily 08:00 Boardroom recap — regenerate Holding CEO synthesis + broadcast
# to the configured Discord channel.
#
# Run by LaunchAgent: ~/Library/LaunchAgents/com.opsdashboard.boardroom.recap.plist
# Runs the same flow as the Web UI's "↻ Regenerate" button: forces a fresh
# synthesis, then pushes it to Discord if DISCORD_WEBHOOK_URL is set.
# Telegram broadcast would need a known chat_id table (planned v1.2).
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
from ops_dashboard import telegram_bridge as tg

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

# 3) Telegram outbound — broadcasts to all bound telegram_chats
try:
    if tg.is_configured():
        bound = tg.list_chats(bound_only=True)
        if not bound:
            print("[cron] telegram: no bound chats — chairman hasn't bound any chat_id yet")
        else:
            print(f"[cron] telegram: broadcasting to {len(bound)} bound chat(s)")
            tmsg = f"**{title}**\n\n{text}"
            res = tg.broadcast_to_bound_chats(tmsg)
            print(f"[cron] telegram broadcast result: {res}")
    else:
        print("[cron] telegram: not configured, skipping")
except Exception as e:
    print(f"[cron] telegram error: {e!r}")
PY

echo "[cron] done at $(date '+%Y-%m-%d %H:%M:%S')"
