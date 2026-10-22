#!/bin/bash
# Daily GA4 + Search Console poll for the dashboard, independent of the app
# being open. Meant to be run by launchd/cron; safe to run any time (upserts
# are idempotent on (site_id, date)). Windows overlap the sources' 1-3 day
# reporting lag on purpose.
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m ops_dashboard.cli ga poll --days 3
.venv/bin/python -m ops_dashboard.cli gsc poll --days 7
