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
# The lockfile carries the crate's own version. `cargo check --locked` (three
# workflows) refuses to build when it disagrees with Cargo.toml, so bumping
# the crate without the lock breaks CI on the next push.
sed -i '' '/^name = "conductloop-desktop"$/,/^$/ s/^version = ".*"$/version = "'"$VERSION"'"/' Cargo.lock
# The Python side carries the version too, and the pre-flight checks both
# (they are FAIL, not WARN). Bumping them here means the release is one
# command and nothing is left at the previous version.
REPO_ROOT="$(cd "$TAURI_DIR/../.." && pwd)"
sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" "$REPO_ROOT/pyproject.toml"
sed -i '' "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" "$REPO_ROOT/ops_dashboard/__init__.py"
sed -i '' "s/\"version\": \".*\"/\"version\": \"$VERSION\"/" "$REPO_ROOT/desktop/package.json"
grep -E "^version" Cargo.toml | head -1
grep -E "\"version\"" tauri.conf.json | head -1
grep -A 1 'name = "conductloop-desktop"' Cargo.lock | grep version | head -1
grep -E "^version" "$REPO_ROOT/pyproject.toml" | head -1
grep -E "^__version__" "$REPO_ROOT/ops_dashboard/__init__.py" | head -1
grep -E "\"version\"" "$REPO_ROOT/desktop/package.json" | head -1
echo ""

# ── 2. Build sidecar ─────────────────────────────────────────────────────────
echo "▸ Building sidecar (PyInstaller)"
# Matches desktop/build-app.sh: freeze from the REPO ROOT venv using the spec
# (the desktop/ dir has no venv/requirements of its own).
cd "$TAURI_DIR/../.."
if [[ ! -x ".venv/bin/pyinstaller" ]]; then
  .venv/bin/pip install --quiet pyinstaller
fi
# The sidecar freezes whatever .venv has installed. Refuse to ship a .venv
# that has drifted below requirements.txt (1.2.22 shipped a vulnerable Pillow
# that way): the floors in that file only protect anyone if they hold here.
.venv/bin/python scripts/check_build_env.py requirements.txt
.venv/bin/pyinstaller desktop/conductloop_server.spec --noconfirm --distpath dist --workpath build/pyi 2>&1 | tail -3
mkdir -p "$TAURI_DIR/binaries"
cp "dist/conductloop-server" "$SIDECAR_DEST"
chmod +x "$SIDECAR_DEST"
ls -la "$SIDECAR_DEST"
echo ""

# ── 3. Build app + bundles ───────────────────────────────────────────────────
# The DMG step asks the Finder to dress the volume, and a Finder that is
# stuck answers "El Finder está en uso (-15260)" and the build dies there.
# It has happened on three releases out of four. Restarting it is cheap and
# reversible (macOS relaunches it); the windows the user had open are
# closed, which is the price of a release that finishes.
echo "▸ Restarting Finder (it gets stuck before the DMG step)"
killall Finder 2>/dev/null || true
sleep 3

echo "▸ Building .app + DMG + updater artifacts"
cd "$TAURI_DIR"
# Panic locations embed absolute source paths (~/.cargo/registry/…, the repo
# itself), which would publish the builder's home directory and username in
# the binary: --remap-path-prefix rewrites them while compiling, so they never
# reach the file.
#
# The build also needs -Wl,-no_fixup_chains. With the deployment target Tauri
# sets (13.0, from bundle.macOS.minimumSystemVersion), rustc asks ld64 for
# chained fixups, and Xcode 27.2's ld64 then lays out __LINKEDIT so that some
# proc-macro dylibs end up with a 4-byte-aligned string table. dyld refuses to
# load those ("mis-aligned LINKEDIT string pool") and rustc dies with "can't
# find crate for `phf_macros`" — an error that names a crate that has nothing
# to do with the problem. The flag keeps the classic dyld info, whose layout is
# aligned. See desktop/src-tauri/.cargo/config.toml.
export RUSTFLAGS="--remap-path-prefix=$HOME=/build -C link-arg=-Wl,-no_fixup_chains"
# The checker below only looks at the artifacts of THIS build.
BUILD_START="$(date +%s)"
TAURI_SIGNING_PRIVATE_KEY="$(cat "$UPDATER_KEY")" \
TAURI_SIGNING_PRIVATE_KEY_PASSWORD="" \
cargo tauri build 2>&1 | tail -10

# A build that produced a dylib dyld cannot load is a build that cannot be
# trusted: the next one dies on the same corrupt artifact with an error about
# an unrelated crate. Check before anything is signed.
#
# Only the artifacts of this build are checked. Cargo leaves the dylibs of
# older builds in the target dir (different flags, different hashes in the
# file name) and those are not what this release ships: rejecting them would
# abort a release whose current build is perfectly fine.
echo "▸ Checking the built Mach-O files are loadable"
python3 "$REPO_ROOT/scripts/check_macho_dylibs.py" \
  --deps-dir "$TAURI_DIR/target/release/deps" \
  --binary "$APP_PATH/Contents/MacOS/conductloop-desktop" \
  --since "$BUILD_START"
# The remap above rewrites the builder's paths while compiling. Nothing of
# $HOME must be left in the binary that ships.
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
# The window background (desktop/src-tauri/dmg/background.png, drawn at 2x
# from desktop/assets/dmg-background.svg) has its arrow between these two
# icon positions: keep them in sync, and with bundle.macOS.dmg in tauri.conf.json.
./bundle_dmg.sh \
  --volname "ConductLoop" \
  --volicon "$APP_PATH/Contents/Resources/icon.icns" \
  --background "$TAURI_DIR/dmg/background.png" \
  --window-pos 200 120 \
  --window-size 600 400 \
  --icon-size 100 \
  --icon "ConductLoop.app" 175 190 \
  --hide-extension "ConductLoop.app" \
  --app-drop-link 425 190 \
  "ConductLoop_${VERSION}_aarch64.dmg" "$APP_PATH" 2>&1 | tail -3
echo ""

# Up to 1.2.1 the DMG itself was not signed (only its notarization ticket was
# stapled): `spctl -t open` rejected it with "no usable signature". Sign it
# with the same Developer ID before notarizing it.
echo "▸ Signing DMG"
codesign --force --timestamp --sign "$IDENTITY" "ConductLoop_${VERSION}_aarch64.dmg"
codesign --verify --verbose=2 "ConductLoop_${VERSION}_aarch64.dmg" 2>&1 | tail -1
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
# What Gatekeeper checks when a downloaded DMG is opened.
if ! spctl -a -t open --context context:primary-signature "ConductLoop_${VERSION}_aarch64.dmg" 2>/dev/null; then
  spctl -a -vv -t open --context context:primary-signature "ConductLoop_${VERSION}_aarch64.dmg" >&2 || true
  echo "✗ Gatekeeper does not accept the DMG — abort" >&2
  exit 1
fi
echo "  ✓ Gatekeeper accepts the DMG"
echo ""

# ── 6. Sign updater tar.gz ───────────────────────────────────────────────────
echo "▸ Signing updater tar.gz"
cd "$RELEASE_BUNDLE/macos"
rm -f ConductLoop.app.tar.gz ConductLoop.app.tar.gz.sig
# COPYFILE_DISABLE / --no-mac-metadata: macOS tar otherwise adds an AppleDouble
# "._" entry for every file with extended attributes (11 in 1.2.1, starting
# with ._ConductLoop.app), and the updater fails on them: "failed to unpack
# `._ConductLoop.app`". `tar -t` on macOS hides those entries, so check with
# Python that the archive holds ConductLoop.app and nothing else.
COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs -czf ConductLoop.app.tar.gz \
  -C "$(dirname "$APP_PATH")" "$(basename "$APP_PATH")"
python3 - ConductLoop.app.tar.gz <<'PY'
import sys, tarfile
names = tarfile.open(sys.argv[1]).getnames()
bad = [n for n in names if n.split("/")[-1].startswith("._") or n.split("/")[0] != "ConductLoop.app"]
if bad:
    sys.exit(f"✗ updater archive has entries the updater cannot unpack: {bad[:5]}")
print(f"  ✓ updater archive: {len(names)} entries, all under ConductLoop.app/, no AppleDouble files")
PY
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
# Starts the built app with a throwaway data folder (never the maintainer's own
# library: the first start of a new version migrates it) and waits for its
# server to answer /api/health as this version. It used to `open` the app on
# the real data, wait 8 s and count lsof lines: lsof finding nothing exits 1,
# which under `set -euo pipefail` ended the script silently, and a first start
# that migrates a large library takes longer than 8 s anyway.
echo "▸ Smoke-testing .app"
if curl -s -m 2 -o /dev/null http://127.0.0.1:7777/api/health; then
  echo "  ✗ Something already answers on 127.0.0.1:7777 (ConductLoop or make serve): quit it and run again" >&2
  exit 1
fi
SMOKE_DIR="$(mktemp -d)"
OPS_DATA_DIR="$SMOKE_DIR/data" OPS_CONFIG_PATH="$SMOKE_DIR/config.json" \
  "$APP_PATH/Contents/MacOS/conductloop-desktop" >/dev/null 2>&1 &
SMOKE_PID=$!
HEALTH=""
for _ in $(seq 1 90); do
  HEALTH="$(curl -s -m 2 http://127.0.0.1:7777/api/health || true)"
  [[ "$HEALTH" == *"\"version\": \"$VERSION\""* ]] && break
  sleep 1
done
# The server exits with the app (parent_watch): no pkill of everything named conductloop.
kill "$SMOKE_PID" 2>/dev/null || true
wait "$SMOKE_PID" 2>/dev/null || true
sleep 2
rm -rf "$SMOKE_DIR"
if [[ "$HEALTH" == *"\"version\": \"$VERSION\""* ]]; then
  echo "  ✓ The app's server answered on 127.0.0.1:7777 as $VERSION"
else
  echo "  ✗ The app's server did not answer as $VERSION within 90 s — abort" >&2
  echo "    last answer: ${HEALTH:-none}" >&2
  exit 1
fi
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