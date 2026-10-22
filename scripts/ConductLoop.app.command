#!/bin/bash
# ConductLoop — ops-dashboard launcher
# Doble-click this file to start the dashboard as a standalone macOS app window.

set -e
cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

echo "🧭 ConductLoop — ops-dashboard"
echo ""

# Check venv
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "✗ no .venv found at $PROJECT_DIR/.venv"
    echo "  run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    read -p "Press enter to exit"
    exit 1
fi

PY="$PROJECT_DIR/.venv/bin/python"

# Check if already running
if lsof -ti:7777 >/dev/null 2>&1; then
    echo "✓ server already running on :7777"
else
    echo "→ starting server on :7777..."
    nohup "$PY" -m ops_dashboard serve --port 7777 > /tmp/ops-dashboard.log 2>&1 &
    SERVER_PID=$!
    # wait for it to come up
    for i in 1 2 3 4 5 6 7 8; do
        sleep 0.5
        if lsof -ti:7777 >/dev/null 2>&1; then
            echo "✓ server up (pid $SERVER_PID)"
            break
        fi
    done
    if ! lsof -ti:7777 >/dev/null 2>&1; then
        echo "✗ server failed to start. check /tmp/ops-dashboard.log"
        read -p "Press enter to exit"
        exit 1
    fi
fi

# Open in Chrome as standalone app window
URL="http://127.0.0.1:7777/"
if [ -d "/Applications/Google Chrome.app" ]; then
    BROWSER="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    echo "→ opening Chrome (standalone window)..."
    "$BROWSER" --app="$URL" --window-size=1400,900 --new-window >/dev/null 2>&1 &
elif [ -d "/Applications/Arc.app" ]; then
    open -a Arc "$URL"
elif [ -d "/Applications/Brave Browser.app" ]; then
    BROWSER="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
    "$BROWSER" --app="$URL" --window-size=1400,900 --new-window >/dev/null 2>&1 &
else
    echo "→ opening default browser..."
    open "$URL"
fi

echo ""
echo "✓ ready. close this window or press Ctrl+C to stop the server."
echo "  (the server keeps running in background. to stop: lsof -ti:7777 | xargs kill)"
echo ""
echo "press any key to close this launcher..."
read -n 1
