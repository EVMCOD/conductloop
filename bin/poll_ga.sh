#!/bin/bash
# Wrapper to poll GA4 sites. Run by LaunchAgent daily.
#
# bin/ops_env.sh decides which code runs, which database is written and where
# the logs go — see the comment there for the incident that made this file.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=bin/ops_env.sh
. "$REPO_ROOT/bin/ops_env.sh"
cd "$OPS_REPO_ROOT"
ops_check_code
"$OPS_PYTHON" -m ops_dashboard.cli ga poll --days 7 >> "$OPS_LOG_DIR/ga_poll.log" 2>&1
