#!/bin/bash
# Wrapper to check live URLs from cron. Logs to the ops-dashboard logs folder.
#
# bin/ops_env.sh decides which code runs, which database is written and where
# the logs go — see the comment there for the incident that made this file.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=bin/ops_env.sh
. "$REPO_ROOT/bin/ops_env.sh"
cd "$OPS_REPO_ROOT"
ops_check_code
"$OPS_PYTHON" -m ops_dashboard.cli index urls >> "$OPS_LOG_DIR/url_watch.log" 2>&1
