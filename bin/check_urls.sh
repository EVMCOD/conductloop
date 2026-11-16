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
# Who this job is and when launchd runs it, so its runs show up in the panel
# (Jobs) next to the app's own: failed, or late when it stopped running. The
# plist can say otherwise with OPS_JOB_SCHEDULE.
export OPS_JOB_NAME=url_watch
export OPS_JOB_SCHEDULE="${OPS_JOB_SCHEDULE:-every 300s}"
"$OPS_PYTHON" -m ops_dashboard.cli index urls >> "$OPS_LOG_DIR/url_watch.log" 2>&1
