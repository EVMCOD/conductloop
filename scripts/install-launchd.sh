#!/bin/bash
# Install (or re-install) the ops-dashboard LaunchAgents into launchd.
# Uses modern `launchctl bootstrap` syntax (macOS 13+) instead of deprecated
# `launchctl load`. The deprecated syntax caused "Input/output error" failures
# for several agents (revenue, ga, digest) which silently never started.
set -e
OPS_HOME="$HOME/ops-dashboard"
AGENT_DIR="$HOME/Library/LaunchAgents"
USER_GUI="gui/$(id -u)"

# Patterns to (re)load — one plist per agent
PLISTS=(
  com.opsdashboard.urlwatch.plist
  com.opsdashboard.revenue.plist
  com.opsdashboard.ga.plist
  com.opsdashboard.digest.plist
  com.opsdashboard.logrotate.plist
  com.opsdashboard.watchdog.plist
)

for plist in "${PLISTS[@]}"; do
  src="$AGENT_DIR/$plist"
  if [ ! -f "$src" ]; then
    echo "skip $plist (file not found)"
    continue
  fi
  label="${plist%.plist}"
  # Bootout first (ignore "not loaded" errors)
  launchctl bootout "$USER_GUI/$label" 2>/dev/null || true
  # Then bootstrap fresh
  if launchctl bootstrap "$USER_GUI" "$src" 2>/dev/null; then
    echo "loaded $label"
  else
    echo "FAILED $label (will try load -w fallback)"
    launchctl load -w "$src" 2>/dev/null && echo "  loaded via legacy load" || echo "  legacy load also failed"
  fi
done

echo ""
echo "=== Final status ==="
launchctl list | grep -i opsdashboard || echo "(no opsdashboard agents listed)"
