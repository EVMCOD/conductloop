#!/bin/bash
# Truncate log files larger than MAX_SIZE (default 1MB).
# Keeps the last MAX_KEEP_LINES lines as a tail.
# Designed to be called from launchd daily or manually.
set -e
OPS_HOME="$HOME/ops-dashboard"
LOG_DIR="$OPS_HOME/logs"
MAX_SIZE=${MAX_SIZE:-1048576}    # 1 MB
MAX_KEEP_LINES=${MAX_KEEP_LINES:-500}

for f in "$LOG_DIR"/*.log; do
  [ -f "$f" ] || continue
  size=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null || echo 0)
  if [ "$size" -gt "$MAX_SIZE" ]; then
    # Tail last N lines, overwrite original
    tail -n "$MAX_KEEP_LINES" "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    echo "rotated $(basename $f): was $size bytes"
  fi
done
