#!/bin/bash
# Wrapper to poll GA4 sites. Run by LaunchAgent daily.
set -e
OPS_HOME="$HOME/ops-dashboard"
LOG_DIR="$OPS_HOME/logs"
mkdir -p "$LOG_DIR"
cd "$OPS_HOME"
# shellcheck disable=SC1091
source .venv/bin/activate
python -m ops_dashboard.cli ga poll --days 7 >> "$LOG_DIR/ga_poll.log" 2>&1
