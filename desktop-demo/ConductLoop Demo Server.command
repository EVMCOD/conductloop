#!/bin/bash
# ConductLoop Demo Server launcher
#
# Starts the local ConductLoop server in DEMO mode (no personal data).
# Once you see "serving on http://127.0.0.1:7778", open the
# ConductLoop Demo.app in this same folder.
#
# The demo server uses a separate port (7778) so it never collides with
# your real ConductLoop server on 7777. Both can run side by side.
#
# Demo data is stored in ~/.conductloop-demo/data.db (separate from your
# real ops-dashboard data). Delete that folder to reset.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

export CONDUCTLOOP_DEMO=1
export CONDUCTLOOP_DB_PATH="$HOME/.conductloop-demo/data.db"
mkdir -p "$(dirname "$CONDUCTLOOP_DB_PATH")"

PY="$DASHBOARD_DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "ERROR: $PY not found."
  echo "Run 'make install' in $DASHBOARD_DIR first."
  exit 1
fi

DEMO_PORT=7778
if lsof -ti:$DEMO_PORT >/dev/null 2>&1; then
  echo "============================================================"
  echo "  WARNING: port $DEMO_PORT is busy"
  echo "============================================================"
  echo "Another process is using port $DEMO_PORT (probably another demo"
  echo "server already running)."
  echo
  lsof -ti:$DEMO_PORT | while read pid; do
    name=$(ps -p $pid -o comm= 2>/dev/null || echo "unknown")
    echo "  PID $pid: $name"
  done
  echo "============================================================"
  exit 1
fi

cd "$DASHBOARD_DIR"

echo "============================================================"
echo "  ConductLoop Demo Server"
echo "============================================================"
echo "DB:        $CONDUCTLOOP_DB_PATH"
echo "Dashboard: http://127.0.0.1:$DEMO_PORT"
echo "============================================================"
echo "Open ConductLoop Demo.app in this folder."
echo "Press Ctrl+C here to stop."
echo

exec "$PY" -m ops_dashboard.cli serve --port $DEMO_PORT
