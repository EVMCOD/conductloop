#!/bin/bash
# Build the ConductLoop sidecar end-to-end. Run from the project root.
#
#   cd <repo root>
#   DEVELOPER_ID="Developer ID Application: <Name> (<TEAMID>)" bash desktop/src-tauri/build-sidecar.sh
#
# This is a manual version of scripts/release-desktop.sh for the sidecar
# bundle specifically. It does NOT notarize/upload — that's done in
# release-desktop.sh after this succeeds.

set -euo pipefail

TAURI_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$TAURI_DIR/../.." && pwd)"

# 1. Build onedir (PyInstaller --onedir, codesign_identity='-' ad-hoc)
echo "▸ Building sidecar (PyInstaller --onedir)..."
.venv/bin/pyinstaller \
  --onedir \
  --name conductloop-server \
  --distpath /tmp/conductloop-onedir \
  --workpath /tmp/conductloop-work \
  --noconfirm --clean \
  "$PROJECT_ROOT/conductloop_server_entry.py"

# 2. Stage at the path Tauri expects
echo "▸ Staging at $TAURI_DIR/binaries/..."
mkdir -p "$TAURI_DIR/binaries"
cp /tmp/conductloop-onedir/conductloop-server \
   "$TAURI_DIR/binaries/conductloop-server-aarch64-apple-darwin"
chmod +x "$TAURI_DIR/binaries/conductloop-server-aarch64-apple-darwin"

# 3. Rebuild the Tauri .app
echo "▸ cargo tauri build..."
cd "$TAURI_DIR"
TAURI_SIGNING_PRIVATE_KEY="$(cat $HOME/.conductloop-keys/conductloop.key)" \
TAURI_SIGNING_PRIVATE_KEY_PASSWORD="" \
cargo tauri build

# 4. Post-build: copy onedir bundle into the .app at Resources/sidecar/
echo "▸ Post-build: copy onedir into .app..."
APP_PATH="$TAURI_DIR/target/release/bundle/macos/ConductLoop.app"
RES="$APP_PATH/Contents/Resources"
SB="$RES/sidecar/conductloop-server-bundle"

if [[ -d "$SB" ]]; then
    rm -rf "$SB"
fi
mkdir -p "$SB"
cp -R /tmp/conductloop-onedir/conductloop-server/. "$SB/"

# 5. Inject a wrapper script (Tauri 2's externalBin path doesn't reliably
# launch onedir binaries with --deep signed sub-resources, so a small
# shell wrapper that exec()s the onedir is more reliable).
cat > "$APP_PATH/Contents/MacOS/conductloop-server" <<'WRAPPER'
#!/bin/sh
exec /Applications/ConductLoop.app/Contents/Resources/sidecar/conductloop-server-bundle/conductloop-server "$@"
WRAPPER
chmod +x "$APP_PATH/Contents/MacOS/conductloop-server"

# 6. Sign the bundle contents (Developer ID + hardened runtime)
LOCAL_ENV="$(cd "$(dirname "$0")/../.." && pwd)/scripts/release.local.env"
# shellcheck disable=SC1090
[[ -f "$LOCAL_ENV" ]] && source "$LOCAL_ENV"
IDENTITY="${DEVELOPER_ID:?set DEVELOPER_ID (e.g. in scripts/release.local.env)}"

echo "▸ Signing internals (Developer ID + hardened runtime)..."
find "$SB/_internal" -type f \( -name "*.so" -o -name "*.dylib" \) | while read f; do
    codesign --force --options runtime --timestamp --sign "$IDENTITY" "$f" 2>/dev/null
done
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
  "$SB/_internal/Python.framework/Versions/3.14/Python" > /dev/null
for f in "$SB/_internal/torch/bin/"*; do
    codesign --force --options runtime --timestamp --sign "$IDENTITY" "$f" 2>/dev/null
done
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
  "$SB/conductloop-server" > /dev/null
codesign --force --timestamp --sign "$IDENTITY" \
  "$APP_PATH/Contents/MacOS/conductloop-server" > /dev/null

echo "▸ Re-signing .app deeply..."
codesign --remove-signature "$APP_PATH" 2>/dev/null
codesign --force --options runtime --timestamp --deep \
  --entitlements "$TAURI_DIR/entitlements.plist" \
  --sign "$IDENTITY" "$APP_PATH" 2>&1 | tail -1

# 7. Run a sanity smoke-test (not notarized; user must do that)
echo ""
echo "✓ Built: $APP_PATH"
echo "  Test: open '$APP_PATH' and wait ~30s for port 7777 to bind"
