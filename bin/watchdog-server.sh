#!/bin/bash
# Watchdog: if the dashboard server :7777 is down, start it.
# Used as a heartbeat by launchd every 60s.
set -e
OPS_HOME="$HOME/ops-dashboard"
LOG_DIR="$OPS_HOME/logs"
mkdir -p "$LOG_DIR"
cd "$OPS_HOME"

# Check if server is responding. Use the dedicated /api/health endpoint: it is
# cheap (no page render) and returns 200 only when the DB is reachable, so a
# server stuck with a dead DB (503) gets recycled instead of looking "up".
if curl -sS -o /dev/null -w "%{http_code}" --max-time 3 http://localhost:7777/api/health 2>/dev/null | grep -q "200"; then
  exit 0  # healthy
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) server down, restarting" >> "$LOG_DIR/watchdog.log"

# Kill any stale process on the port
lsof -ti:7777 2>/dev/null | xargs -r kill -9 2>/dev/null || true
sleep 1

# Start the server detached from this script
nohup .venv/bin/python -m ops_dashboard serve --port 7777 > "$LOG_DIR/server.out.log" 2>&1 &
disown
