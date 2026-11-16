#!/bin/bash
# Daily GA4 + Search Console poll for the dashboard, independent of the app
# being open. Meant to be run by launchd/cron; safe to run any time (upserts
# are idempotent on (site_id, date)). Windows overlap the sources' 1-3 day
# reporting lag on purpose.
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
export OPS_JOB_NAME=analytics_poll
export OPS_JOB_SCHEDULE="${OPS_JOB_SCHEDULE:-30 8 * * *}"
"$OPS_PYTHON" -m ops_dashboard.cli ga poll --days 3
"$OPS_PYTHON" -m ops_dashboard.cli gsc poll --days 7
