#!/usr/bin/env bash
# Smoke-test a Linux build of the desktop app: start it on a display with an
# empty HOME, and check that it comes up the way a new user would see it.
#
#   scripts/linux-smoke.sh deb      path/to/ConductLoop_<v>_amd64.deb
#   scripts/linux-smoke.sh appimage path/to/ConductLoop_<v>_amd64.AppImage
#
# Checks, in order (each prints ✓ or ✗; the exit status is 1 if any failed):
#   1. the local server answers /api/health on 127.0.0.1:7777 within $WAIT s;
#   2. /api/platform says Linux, with the in-app browser off;
#   3. it is a first run: /api/onboarding/state is fresh and /welcome is served;
#   4. a ConductLoop window is on the display (needs xwininfo; skipped without);
#   5. closing the app stops its server (the port is free within 15 s).
# A screenshot of the display after start-up goes to $OUT/linux-smoke-<mode>.png
# (needs ImageMagick's `import`) and the app's output to linux-smoke-<mode>.log.
#
# The .deb is installed with `sudo apt-get install`. The AppImage runs with
# --appimage-extract-and-run, so it needs no FUSE. Without $DISPLAY the script
# starts Xvfb on :99. Everything the app writes goes to a temporary HOME (data,
# config, logs); there is no D-Bus session, so no keyring either: the app must
# say so and keep working. Publishes nothing.
#
# Needs: Linux x86_64, curl, python3, Xvfb when there is no display; optional:
# xwininfo (x11-utils) and import (imagemagick).
set -euo pipefail

MODE="${1:-}"
PKG="${2:-}"
OUT="${OUT:-$PWD}"
WAIT="${WAIT:-120}"
BASE="http://127.0.0.1:7777"

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }
case "$MODE" in
  deb|appimage) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
if [[ -z "$PKG" || ! -f "$PKG" ]]; then
  echo "✗ no package at '$PKG'" >&2
  exit 2
fi
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "✗ linux-smoke.sh must run on Linux (this is $(uname -s))" >&2
  exit 2
fi
PKG="$(realpath "$PKG")"
mkdir -p "$OUT"
LOG="$OUT/linux-smoke-$MODE.log"
SHOT="$OUT/linux-smoke-$MODE.png"
failures=0
report() { echo "$1"; [[ -n "${GITHUB_STEP_SUMMARY:-}" ]] && echo "- $1" >> "$GITHUB_STEP_SUMMARY"; return 0; }
ok() { report "✓ $1"; }
bad() { report "✗ $1"; failures=$((failures + 1)); }

if curl -s -m 2 -o /dev/null "$BASE/api/health"; then
  echo "✗ something already answers on $BASE: stop it first" >&2
  exit 2
fi

case "$MODE" in
  deb)
    # The log belongs to the user running the script, not to root.
    # shellcheck disable=SC2024
    sudo apt-get install -y "$PKG" > "$OUT/linux-smoke-apt.log" 2>&1 || {
      tail -20 "$OUT/linux-smoke-apt.log"; echo "✗ apt could not install $PKG" >&2; exit 1; }
    BIN="$(dpkg-deb -c "$PKG" | awk '{print $NF}' | sed 's#^\./#/#' | grep '/conductloop-desktop$' | head -1)"
    ARGS=()
    ;;
  appimage)
    chmod +x "$PKG"
    BIN="$PKG"
    ARGS=(--appimage-extract-and-run)
    ;;
esac
[[ -n "$BIN" && -x "$BIN" ]] || { echo "✗ no conductloop-desktop binary in $PKG" >&2; exit 1; }
# shellcheck source=/dev/null
[[ -n "${GITHUB_STEP_SUMMARY:-}" ]] && echo "### Linux smoke test: $MODE ($(. /etc/os-release && echo "$PRETTY_NAME"))" >> "$GITHUB_STEP_SUMMARY"

TMP="$(mktemp -d)"
XVFB_PID=""
APP_PID=""
cleanup() {
  [[ -n "$APP_PID" ]] && kill "$APP_PID" 2>/dev/null || true
  pkill -f conductloop-desktop 2>/dev/null || true
  pkill -f conductloop-server 2>/dev/null || true
  [[ -n "$XVFB_PID" ]] && kill "$XVFB_PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

if [[ -z "${DISPLAY:-}" ]]; then
  Xvfb :99 -screen 0 1280x800x24 -nolisten tcp > "$OUT/linux-smoke-xvfb.log" 2>&1 &
  XVFB_PID=$!
  export DISPLAY=:99
  sleep 2
fi

# A new user: empty HOME, no session bus (so no Secret Service), nothing set.
# WEBKIT_DISABLE_DMABUF_RENDERER: Xvfb has no GPU device for WebKitGTK's DMA-BUF
# renderer, which otherwise draws an empty window there.
mkdir -p "$TMP/home"
started=$(date +%s)
env -i PATH="/usr/local/bin:/usr/bin:/bin" HOME="$TMP/home" DISPLAY="$DISPLAY" \
  LANG=C.UTF-8 NO_AT_BRIDGE=1 WEBKIT_DISABLE_DMABUF_RENDERER=1 \
  TMPDIR="$TMP" "$BIN" "${ARGS[@]}" > "$LOG" 2>&1 &
APP_PID=$!

# 1. The server comes up.
up=""
for _ in $(seq 1 "$WAIT"); do
  if curl -s -m 2 -o /dev/null -w '%{http_code}' "$BASE/api/health" | grep -q '^200$'; then up=1; break; fi
  kill -0 "$APP_PID" 2>/dev/null || break
  sleep 1
done
if [[ -n "$up" ]]; then
  version="$(curl -s "$BASE/api/health" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version"))')"
  ok "server answered in $(( $(date +%s) - started )) s as $version"
else
  bad "the server did not answer within $WAIT s (app $(kill -0 "$APP_PID" 2>/dev/null && echo running || echo exited))"
  tail -40 "$LOG" || true
  exit 1
fi

# 2. It knows it is on Linux.
if curl -s "$BASE/api/platform" | python3 -c '
import json, sys
p = json.load(sys.stdin)
for k in ("browser", "code_execution", "keychain"):
    v = p.get(k) or {}
    print(("    %s: available=%s %s %s" % (k, v.get("available"), v.get("reason") or "", v.get("message") or "")).rstrip())
sys.exit(0 if p.get("platform") == "linux" and (p.get("browser") or {}).get("available") is False else 1)
'; then
  ok "/api/platform: linux, in-app browser off"
else
  bad "/api/platform does not say linux with the browser off"
fi

# 3. A first run.
if curl -s "$BASE/api/onboarding/state" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("fresh") is True else 1)' \
   && curl -s "$BASE/welcome" | grep -q "ConductLoop"; then
  ok "first run: onboarding is fresh and /welcome is served"
else
  bad "first run: onboarding not fresh or /welcome missing"
fi

# 4. A window, and what it shows.
sleep 8
if command -v xwininfo >/dev/null; then
  if xwininfo -root -tree | grep -q '"ConductLoop"'; then ok "a ConductLoop window is on the display"; else bad "no ConductLoop window on the display"; fi
fi
if command -v import >/dev/null; then
  import -window root "$SHOT" 2>/dev/null && echo "    screenshot: $SHOT" || echo "    (no screenshot)"
fi

# 5. Closing the app stops its server.
pkill -TERM -f conductloop-desktop 2>/dev/null || true
kill "$APP_PID" 2>/dev/null || true
freed=""
for i in $(seq 1 15); do
  if ! curl -s -m 1 -o /dev/null "$BASE/api/health"; then freed=$i; break; fi
  sleep 1
done
if [[ -n "$freed" ]] && ! pgrep -f conductloop-server >/dev/null; then
  ok "closing the app stopped its server (${freed} s)"
else
  bad "the server kept running after the app was closed"
fi
APP_PID=""

if (( failures )); then
  echo "--- last lines of the app's output"
  tail -40 "$LOG" || true
  exit 1
fi
