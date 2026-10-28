#!/usr/bin/env bash
# Build the ConductLoop desktop .app / .dmg:
#   1. Freeze the server into a single-file sidecar binary (PyInstaller).
#   2. Copy it into src-tauri/binaries/ with the target-triple name Tauri wants.
#   3. Build the Tauri app (.app + .dmg).
#
# Prereqs: the repo's .venv with pyinstaller, Rust toolchain, and
# `npm install` already run in desktop/.
#
# Code signing + notarization (needed for distribution outside your Mac) is a
# separate step — see scripts/release-desktop.sh.
set -eo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DESKTOP="$REPO/desktop"
# Detect target triple. Fallback to a hard-coded aarch64-apple-darwin if
# rustc isn't in PATH (e.g. when this script runs from a non-interactive
# shell that hasn't sourced the cargo/rustup env).
TRIPLE="$(rustc -vV 2>/dev/null | sed -n 's/host: //p')"
TRIPLE="${TRIPLE:-aarch64-apple-darwin}"

echo "▸ 1/3  Freezing the server (PyInstaller)…"
cd "$REPO"
.venv/bin/pyinstaller desktop/conductloop_server.spec --noconfirm --distpath dist --workpath build/pyi

echo "▸ 2/3  Placing sidecar binary for triple $TRIPLE…"
mkdir -p "$DESKTOP/src-tauri/binaries"
cp "$REPO/dist/conductloop-server" "$DESKTOP/src-tauri/binaries/conductloop-server-$TRIPLE"
chmod +x "$DESKTOP/src-tauri/binaries/conductloop-server-$TRIPLE"

echo "▸ 3/3  Building the Tauri app (.app + .dmg)…"
cd "$DESKTOP"
npm run tauri build

echo "✓ Done. Artifacts in:"
echo "   $DESKTOP/src-tauri/target/release/bundle/dmg/*.dmg"
echo "   $DESKTOP/src-tauri/target/release/bundle/macos/*.app"

echo ""
echo "Signing, notarizing and publishing: scripts/release-desktop.sh (see desktop/docs/DISTRIBUTION.md)."
