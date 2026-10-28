#!/usr/bin/env bash
# release-desktop.sh — Build, sign, notarize, and publish a new ConductLoop
# macOS desktop release to https://github.com/EVMCOD/conductloop-releases.
#
# Usage: ./scripts/release-desktop.sh 1.0.1
#
# Prereqs:
#   - scripts/release.local.env (gitignored; copy release.local.env.example):
#     DEVELOPER_ID, NOTARY_KEY, NOTARY_KEY_ID, NOTARY_ISSUER
#   - your Developer ID Application cert in the Keychain
#   - your App Store Connect API key (.p8) for notarytool
#   - ConductLoop updater signing key in ~/.conductloop-keys/conductloop.key
#   - cargo tauri + xcode CLI tools
#   - gh CLI authenticated
#
# What it does:
#   1. Updates version in Cargo.toml + tauri.conf.json
#   2. Builds sidecar (PyInstaller)
#   3. Builds .app + DMG + updater tar.gz (cargo tauri build)
#   4. Re-signs sidecar + .app with Developer ID + hardened runtime
#   5. Re-bundles DMG, signs it, notarizes via notarytool, staples
#   6. Signs the updater tar.gz with the updater key
#   7. Generates update-macos-aarch64.json manifest
#   8. Smoke-tests the .app
#   9. Uploads all artifacts to the GitHub release tag (creates if missing)
#  10. Smoke-tests the download URL + manifest

set -euo pipefail

VERSION="${1:?usage: release-desktop.sh VERSION (e.g. 1.0.1)}"
REPO="EVMCOD/conductloop-releases"
TAG="v${VERSION}"

# Config: the maintainer's signing identity and notarization key live outside
# the repository, in scripts/release.local.env (gitignored).
LOCAL_ENV="$(cd "$(dirname "$0")" && pwd)/release.local.env"
# shellcheck disable=SC1090
[[ -f "$LOCAL_ENV" ]] && source "$LOCAL_ENV"
IDENTITY="${DEVELOPER_ID:?set DEVELOPER_ID (e.g. in scripts/release.local.env)}"
NOTARY_KEY="${NOTARY_KEY:?set NOTARY_KEY (path to your AuthKey_XXXX.p8)}"
NOTARY_KEY_ID="${NOTARY_KEY_ID:?set NOTARY_KEY_ID}"
NOTARY_ISSUER="${NOTARY_ISSUER:?set NOTARY_ISSUER}"
UPDATER_KEY="$HOME/.conductloop-keys/conductloop.key"
TAURI_DIR="$(cd "$(dirname "$0")/../desktop/src-tauri" && pwd)"
RELEASE_BUNDLE="$TAURI_DIR/target/release/bundle"
DMG_PATH="$RELEASE_BUNDLE/dmg/ConductLoop_${VERSION}_aarch64.dmg"
APP_PATH="$RELEASE_BUNDLE/macos/ConductLoop.app"
SIDECAR_SRC="$TAURI_DIR/../dist/conductloop-server"
SIDECAR_DEST="$TAURI_DIR/binaries/conductloop-server-aarch64-apple-darwin"
# Public half of UPDATER_KEY. Only this .pub is read by the key check below.
UPDATER_PUBKEY_FILE="${UPDATER_PUBKEY_FILE:-$UPDATER_KEY.pub}"
KEY_CHECK="$(cd "$(dirname "$0")" && pwd)/check_updater_pubkey.py"

cd "$TAURI_DIR"

echo "════════════════════════════════════════════════════════"
echo "  ConductLoop release $VERSION"
echo "════════════════════════════════════════════════════════"
echo ""

# ── 0. Updater key sanity (before building/signing anything) ────────────────
# Installed apps only accept updates signed by the key embedded in
# tauri.conf.json. v1.1.1/v1.2.0 shipped with a pubkey (D68774E13CB63DED) that
# didn't match the signing key, so their auto-update could never verify.
echo "▸ Checking updater pubkey matches the signing key"
python3 "$KEY_CHECK" --config "$TAURI_DIR/tauri.conf.json" \
  --pubkey-file "$UPDATER_PUBKEY_FILE" \
  --expected-file "$TAURI_DIR/../UPDATER_KEY_ID"
echo ""

# ── 1. Update version ────────────────────────────────────────────────────────
echo "▸ Updating version to $VERSION"
sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" Cargo.toml
sed -i '' "s/\"version\": \".*\"/\"version\": \"$VERSION\"/" tauri.conf.json
grep -E "^version" Cargo.toml | head -1
grep -E "\"version\"" tauri.conf.json | head -1
echo ""

# ── 2. Build sidecar ─────────────────────────────────────────────────────────
echo "▸ Building sidecar (PyInstaller)"
# Matches desktop/build-app.sh: freeze from the REPO ROOT venv using the spec
# (the desktop/ dir has no venv/requirements of its own).
cd "$TAURI_DIR/../.."
if [[ ! -x ".venv/bin/pyinstaller" ]]; then
  .venv/bin/pip install --quiet pyinstaller
fi
.venv/bin/pyinstaller desktop/conductloop_server.spec --noconfirm --distpath dist --workpath build/pyi 2>&1 | tail -3
mkdir -p "$TAURI_DIR/binaries"
cp "dist/conductloop-server" "$SIDECAR_DEST"
chmod +x "$SIDECAR_DEST"
ls -la "$SIDECAR_DEST"
echo ""

# ── 3. Build app + bundles ───────────────────────────────────────────────────
echo "▸ Building .app + DMG + updater artifacts"
cd "$TAURI_DIR"
# Panic locations embed absolute source paths (~/.cargo/registry/…, ~/.rustup/…),
# which would publish the builder's home directory and username in the binary.
TAURI_SIGNING_PRIVATE_KEY="$(cat "$UPDATER_KEY")" \
TAURI_SIGNING_PRIVATE_KEY_PASSWORD="" \
RUSTFLAGS="${RUSTFLAGS:-} --remap-path-prefix=$HOME=/build" \
cargo tauri build 2>&1 | tail -10
if strings -a "$APP_PATH/Contents/MacOS/conductloop-desktop" | grep -q "$HOME"; then
  echo "✗ the desktop binary still contains $HOME" >&2
  exit 1
fi
# The in-app browser's proxy needs Network.framework symbols that only exist on
# macOS 14+. They must stay weak imports or the app won't launch on macOS 13.
if nm -m "$APP_PATH/Contents/MacOS/conductloop-desktop" | grep "(from Network)" | grep -vq "weak external"; then
  echo "✗ Network.framework symbols are strongly linked (app would not launch on macOS < 14)" >&2
  exit 1
fi
echo ""

# ── 4. Re-sign sidecar + .app ────────────────────────────────────────────────
echo "▸ Re-signing sidecar + .app with hardened runtime"
# The PyInstaller onefile sidecar extracts and dlopen()s a python.org-signed
# Python.framework (different Team ID) at runtime. Under hardened runtime that
# fails ("different Team IDs") unless the signature carries
# disable-library-validation. The entitlements MUST be passed to codesign —
# having the plist on disk does nothing on its own.
ENTITLEMENTS="$TAURI_DIR/entitlements.plist"
codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" \
  --sign "$IDENTITY" "$APP_PATH/Contents/MacOS/conductloop-server"
codesign --force --options runtime --timestamp --deep --entitlements "$ENTITLEMENTS" \
  --sign "$IDENTITY" "$APP_PATH"
codesign -dvvv "$APP_PATH" 2>&1 | grep -E "Identifier|hardcoded" | head -3
echo ""

# ── 4b. Notarize + staple the .app itself ────────────────────────────────────
# Stapling only the DMG leaves the copied-out .app without a local ticket, so a
# first launch with no network can't verify notarization and Gatekeeper blocks
# it. Stapling here means the DMG is built around an already-stapled app.
echo "▸ Notarizing the app itself (so the ticket travels inside the DMG)"
APP_ZIP="${TMPDIR:-/tmp}/ConductLoop_${VERSION}_notarize.zip"
rm -f "$APP_ZIP"
ditto -c -k --keepParent "$APP_PATH" "$APP_ZIP"
xcrun notarytool submit "$APP_ZIP" \
  --key "$NOTARY_KEY" \
  --key-id "$NOTARY_KEY_ID" \
  --issuer "$NOTARY_ISSUER" \
  --wait 2>&1 | tail -4
rm -f "$APP_ZIP"
xcrun stapler staple "$APP_PATH" 2>&1 | tail -2
xcrun stapler validate "$APP_PATH" 2>&1 | tail -1
echo ""

# ── 5. Re-bundle + sign + notarize + staple DMG ──────────────────────────────
echo "▸ Re-bundling DMG"
cd "$RELEASE_BUNDLE/dmg"
rm -f "ConductLoop_${VERSION}_aarch64.dmg"
./bundle_dmg.sh \
  --volname "ConductLoop" \
  --volicon "$APP_PATH/Contents/Resources/icon.icns" \
  --window-pos 200 120 \
  --window-size 600 400 \
  --icon-size 100 \
  --icon "ConductLoop.app" 175 190 \
  --hide-extension "ConductLoop.app" \
  --app-drop-link 425 190 \
  "ConductLoop_${VERSION}_aarch64.dmg" "$APP_PATH" 2>&1 | tail -3
echo ""

echo "▸ Notarizing DMG (this takes ~30s)"
xcrun notarytool submit "ConductLoop_${VERSION}_aarch64.dmg" \
  --key "$NOTARY_KEY" \
  --key-id "$NOTARY_KEY_ID" \
  --issuer "$NOTARY_ISSUER" \
  --wait 2>&1 | tail -4
echo ""

echo "▸ Stapling"
xcrun stapler staple "ConductLoop_${VERSION}_aarch64.dmg" 2>&1 | tail -2
xcrun stapler validate "ConductLoop_${VERSION}_aarch64.dmg" 2>&1 | tail -1
echo ""

# ── 6. Sign updater tar.gz ───────────────────────────────────────────────────
echo "▸ Signing updater tar.gz"
cd "$RELEASE_BUNDLE/macos"
rm -f ConductLoop.app.tar.gz ConductLoop.app.tar.gz.sig
tar -czf ConductLoop.app.tar.gz -C "$(dirname "$APP_PATH")" "$(basename "$APP_PATH")"
TAURI_SIGNING_PRIVATE_KEY="$(cat "$UPDATER_KEY")" \
TAURI_SIGNING_PRIVATE_KEY_PASSWORD="" \
cargo tauri signer sign ConductLoop.app.tar.gz 2>&1 | tail -2
cp ConductLoop.app.tar.gz ConductLoop-${VERSION}.app.tar.gz
cp ConductLoop.app.tar.gz.sig ConductLoop-${VERSION}.app.tar.gz.sig
# The produced signature must come from the key the app embeds.
python3 "$KEY_CHECK" --config "$TAURI_DIR/tauri.conf.json" --sig-file ConductLoop.app.tar.gz.sig
echo ""

# ── 7. Generate manifest ─────────────────────────────────────────────────────
echo "▸ Generating update manifest"
SIG=$(cat ConductLoop.app.tar.gz.sig)
cat > update-macos-aarch64.json <<EOF
{
  "version": "$VERSION",
  "pub_date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "platforms": {
    "darwin-aarch64": {
      "signature": "$SIG",
      "url": "https://github.com/$REPO/releases/download/$TAG/ConductLoop-${VERSION}.app.tar.gz"
    }
  }
}
EOF
echo ""

# ── 8. Smoke-test the .app ───────────────────────────────────────────────────
echo "▸ Smoke-testing .app"
pkill -9 -f conductloop 2>/dev/null || true
sleep 1
open "$APP_PATH"
sleep 8
PORT_OK=$(lsof -nP -iTCP:7777 -sTCP:LISTEN 2>&1 | wc -l | tr -d ' ')
if [[ "$PORT_OK" -gt 0 ]]; then
  echo "  ✓ Sidecar bound 127.0.0.1:7777"
  curl -s -m 5 http://127.0.0.1:7777/api/health | head -c 300
  echo ""
else
  echo "  ✗ Sidecar did NOT bind — abort"
  exit 1
fi
osascript -e 'tell application "ConductLoop" to quit' 2>/dev/null || true
sleep 2
pkill -9 -f conductloop 2>/dev/null || true
echo ""

# ── 9. Upload to GitHub release ──────────────────────────────────────────────
echo "▸ Publishing to GitHub release $TAG"
# Stage every artifact into macos/ (the current cwd) under the canonical names
# the upload expects. Tauri names the DMG ConductLoop_<v>_aarch64.dmg in dmg/,
# but the release/website use ConductLoop-<v>.dmg — copy+rename here so the
# upload never misses. The stable-named copy backs releases/latest/download.
cp "$DMG_PATH" "ConductLoop-${VERSION}.dmg"
cp "ConductLoop-${VERSION}.dmg" "ConductLoop-macos-aarch64.dmg"
if ! gh release view "$TAG" -R "$REPO" >/dev/null 2>&1; then
  gh release create "$TAG" -R "$REPO" \
    --title "ConductLoop $VERSION" \
    --notes "ConductLoop for macOS (Apple Silicon). Signed + notarized.

**What's in this build:**
- Native Tauri 2 bundle (Python server runs as a local sidecar at localhost:7777)
- Local-first: your second brain and AI agents never leave your Mac
- Free edition: every feature unlocked, no subscription

**Install:**
1. Download \`ConductLoop-${VERSION}.dmg\`
2. Open the DMG, drag \`ConductLoop\` to \`/Applications\`
3. Launch — your browser will open to the dashboard automatically
4. That's it — ConductLoop is free

**Requirements:**
- macOS 13 (Ventura) or later (the built-in browser needs macOS 14 Sonoma or later)
- Apple Silicon (M1/M2/M3/M4)" \
    "ConductLoop-${VERSION}.dmg" \
    "ConductLoop-macos-aarch64.dmg" \
    "ConductLoop-${VERSION}.app.tar.gz" \
    "ConductLoop-${VERSION}.app.tar.gz.sig" \
    "update-macos-aarch64.json" 2>&1 | tail -3
else
  gh release upload "$TAG" -R "$REPO" --clobber \
    "ConductLoop-${VERSION}.dmg" \
    "ConductLoop-macos-aarch64.dmg" \
    "ConductLoop-${VERSION}.app.tar.gz" \
    "ConductLoop-${VERSION}.app.tar.gz.sig" \
    "update-macos-aarch64.json" 2>&1 | tail -3
fi
echo ""

# ── 10. Verify URLs ──────────────────────────────────────────────────────────
echo "▸ Verifying download URLs"
sleep 3
DMG_URL="https://github.com/$REPO/releases/download/$TAG/ConductLoop-${VERSION}.dmg"
TAR_URL="https://github.com/$REPO/releases/download/$TAG/ConductLoop-${VERSION}.app.tar.gz"
MANIFEST_URL="https://github.com/$REPO/releases/latest/download/update-macos-aarch64.json"
DMG_CODE=$(curl -s -L -o /dev/null -w "%{http_code}" "$DMG_URL")
TAR_CODE=$(curl -s -L -o /dev/null -w "%{http_code}" "$TAR_URL")
MANIFEST_VERSION=$(curl -s -L "$MANIFEST_URL" | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])" 2>/dev/null || echo "?")
echo "  DMG       ($DMG_URL): $DMG_CODE"
echo "  tar.gz    ($TAR_URL): $TAR_CODE"
echo "  Manifest  latest version: $MANIFEST_VERSION"
echo ""

echo "════════════════════════════════════════════════════════"
echo "  ✓ ConductLoop $VERSION released"
echo "════════════════════════════════════════════════════════"
echo ""
echo "Download: $DMG_URL"