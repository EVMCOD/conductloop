#!/bin/bash
# Wrapper to check live URLs from cron. Logs to ops-dashboard/logs/url_watch.log
set -e
OPS_HOME="$HOME/ops-dashboard"
LOG_DIR="$OPS_HOME/logs"
mkdir -p "$LOG_DIR"
cd "$OPS_HOME"
# shellcheck disable=SC1091
source .venv/bin/activate
python -m ops_dashboard.cli index urls >> "$LOG_DIR/url_watch.log" 2>&1
