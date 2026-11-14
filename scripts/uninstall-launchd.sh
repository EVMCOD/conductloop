#!/bin/bash
# Remove the daily ops-dashboard LaunchAgent.

set -euo pipefail
PLIST_NAME="com.opsdashboard.daily"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

if launchctl list | grep -q "$PLIST_NAME"; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi
rm -f "$PLIST_PATH"
echo "✓ Removed $PLIST_NAME"
