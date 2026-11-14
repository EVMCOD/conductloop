#!/bin/bash
# Wrapper to build + send the daily digest. Run by LaunchAgent every day.
set -e
OPS_HOME="$HOME/ops-dashboard"
LOG_DIR="$OPS_HOME/logs"
mkdir -p "$LOG_DIR"
cd "$OPS_HOME"
# shellcheck disable=SC1091
source .venv/bin/activate
python -m ops_dashboard.cli digest >> "$LOG_DIR/digest.log" 2>&1
