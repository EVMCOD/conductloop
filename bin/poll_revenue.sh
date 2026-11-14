#!/bin/bash
# Wrapper to poll revenue sources. Run by LaunchAgent every hour.
set -e
OPS_HOME="$HOME/ops-dashboard"
LOG_DIR="$OPS_HOME/logs"
mkdir -p "$LOG_DIR"
cd "$OPS_HOME"
# shellcheck disable=SC1091
source .venv/bin/activate
python -m ops_dashboard.cli revenue poll >> "$LOG_DIR/revenue_poll.log" 2>&1
