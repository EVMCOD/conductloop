#!/bin/bash
# Install the daily 08:00 Boardroom recap LaunchAgent. Run once after cloning
# the repo or whenever the user wants to enable the cron.
#
#   bash bin/install_recap_cron.sh install    # creates + loads the plist
#   bash bin/install_recap_cron.sh uninstall  # removes it
#   bash bin/install_recap_cron.sh status     # show current state
#
# LaunchAgent location: ~/Library/LaunchAgents/com.opsdashboard.boardroom.recap.plist
# This is a per-user macOS LaunchAgent, runs even when no terminal is logged in.

set -e

OPS_HOME="$HOME/ops-dashboard"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
LABEL="com.opsdashboard.boardroom.recap"
PLIST="$LAUNCH_DIR/$LABEL.plist"

action="${1:-install}"

case "$action" in
  install)
    mkdir -p "$LAUNCH_DIR"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$OPS_HOME/bin/run_recap_cron.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>8</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key>
    <string>$OPS_HOME/logs/recap_cron.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$OPS_HOME/logs/recap_cron.stderr.log</string>
    <key>WorkingDirectory</key><string>$OPS_HOME</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key><string>$HOME</string>
    </dict>
</dict>
</plist>
EOF
    plutil -lint "$PLIST" || { echo "✗ plist syntax error"; exit 1; }
    launchctl load "$PLIST" 2>&1 || true
    echo "✓ installed and loaded: $PLIST"
    echo "  Next run: 08:00 local time daily"
    ;;
  uninstall)
    if [ -f "$PLIST" ]; then
      launchctl unload "$PLIST" 2>/dev/null || true
      rm -f "$PLIST"
      echo "✓ removed $PLIST"
    else
      echo "(nothing to uninstall)"
    fi
    ;;
  status)
    if [ -f "$PLIST" ]; then
      echo "plist: $PLIST"
      plutil -lint "$PLIST"
    else
      echo "plist: NOT installed at $PLIST"
    fi
    echo ""
    echo "launchd status:"
    launchctl list 2>&1 | grep -i "$LABEL" || echo "  (not currently loaded)"
    ;;
  *)
    echo "usage: $0 {install|uninstall|status}"
    exit 1
    ;;
esac
