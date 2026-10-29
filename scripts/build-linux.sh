#!/usr/bin/env bash
# Build the Linux packages of the desktop app: an AppImage and a .deb.
#
#   scripts/build-linux.sh [--sidecar-only | --skip-sidecar] [-h|--help]
#
# What it does, in order:
#   1. Freezes the local server (desktop/conductloop_server.spec) with
#      PyInstaller, in a venv of its own under build/linux-venv, and copies it
#      to desktop/src-tauri/binaries/conductloop-server-x86_64-unknown-linux-gnu,
#      the name Tauri expects for an externalBin on this target (-<triple>).
#   2. Runs `tauri build --bundles appimage,deb` from desktop/. The Linux-only
#      settings live in desktop/src-tauri/tauri.linux.conf.json, which Tauri
#      merges over tauri.conf.json on Linux only; macOS builds do not read it.
#
# Output: desktop/src-tauri/target/release/bundle/{appimage,deb}/
#
# There is no signing and no updater on Linux: the override turns off the
# updater artifacts, so no signing key is needed, and any TAURI_SIGNING_*
# variable in the environment is dropped. This script never publishes anything.
# It does not touch scripts/release-desktop.sh (the macOS release).
#
#   --sidecar-only  stop after step 1 (the part that needs no WebKitGTK).
#   --skip-sidecar  reuse the sidecar already in desktop/src-tauri/binaries/.
#
# Needs: Linux x86_64, Python >= 3.13 with its shared library (PYTHON=python3.14
# to pick one; actions/setup-python and uv builds have it, Debian's python3 needs
# the libpython3.X package, or PyInstaller stops with an explicit message), Node/npm,
# a Rust toolchain, and the Tauri build dependencies (WebKitGTK 4.1, GTK 3,
# libayatana-appindicator3, librsvg2, patchelf; on Debian/Ubuntu:
#   sudo apt-get install -y libwebkit2gtk-4.1-dev libgtk-3-dev \
#     libayatana-appindicator3-dev librsvg2-dev patchelf
# ).
#
# The AppImage and the frozen server need at least the glibc of the machine
# that builds them: build on the oldest distribution you want to support.
set -euo pipefail

TRIPLE="x86_64-unknown-linux-gnu"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAURI_DIR="$ROOT/desktop/src-tauri"
SIDECAR_DEST="$TAURI_DIR/binaries/conductloop-server-$TRIPLE"
VENV="${CONDUCTLOOP_LINUX_VENV:-$ROOT/build/linux-venv}"
PYTHON="${PYTHON:-python3}"

mode=all
for arg in "$@"; do
  case "$arg" in
    --sidecar-only) mode=sidecar ;;
    --skip-sidecar) mode=bundle ;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Linux" || "$(uname -m)" != "x86_64" ]]; then
  echo "✗ build-linux.sh builds $TRIPLE packages and must run on Linux x86_64 (this is $(uname -s) $(uname -m))" >&2
  exit 1
fi

cd "$ROOT"

# ── 1. Sidecar ───────────────────────────────────────────────────────────────
if [[ "$mode" != "bundle" ]]; then
  echo "▸ Building the sidecar (PyInstaller) for $TRIPLE"
  if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)'; then
    echo "✗ $PYTHON is older than 3.13 (pyproject.toml: requires-python >= 3.13); set PYTHON=" >&2
    exit 1
  fi
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  # The same dependencies the macOS release freezes, minus sentence-transformers
  # (it pulls in torch, which the spec excludes: embeddings run on ONNX).
  mkdir -p build
  grep -vE '^[[:space:]]*(#|-r|sentence-transformers|$)' requirements.txt \
    | sed 's/[[:space:]]*#.*//' > build/linux-sidecar-requirements.txt
  "$VENV/bin/python" -m pip install --quiet -r requirements-core.txt \
    -r build/linux-sidecar-requirements.txt pyinstaller
  "$VENV/bin/pyinstaller" desktop/conductloop_server.spec --noconfirm \
    --distpath dist --workpath build/pyi
  mkdir -p "$TAURI_DIR/binaries"
  cp dist/conductloop-server "$SIDECAR_DEST"
  chmod +x "$SIDECAR_DEST"
  ls -la "$SIDECAR_DEST"
fi
[[ "$mode" == "sidecar" ]] && exit 0

if [[ ! -x "$SIDECAR_DEST" ]]; then
  echo "✗ $SIDECAR_DEST is missing: run without --skip-sidecar first" >&2
  exit 1
fi

# ── 2. Bundles ───────────────────────────────────────────────────────────────
echo "▸ Building the AppImage and the .deb (tauri build)"
cd "$ROOT/desktop"
npm ci --no-audit --no-fund
# No signing on Linux: make sure a key left in the environment is not used.
unset TAURI_SIGNING_PRIVATE_KEY TAURI_SIGNING_PRIVATE_KEY_PASSWORD
# Panic locations embed absolute source paths (~/.cargo/registry/…): do not
# publish the builder's home directory in the binary (same as the macOS release).
# The AppImage tools are AppImages themselves: run them without FUSE.
RUSTFLAGS="${RUSTFLAGS:-} --remap-path-prefix=$HOME=/build" \
APPIMAGE_EXTRACT_AND_RUN=1 \
  npm run --silent tauri -- build --bundles appimage,deb

BIN="$TAURI_DIR/target/release/conductloop-desktop"
if [[ -f "$BIN" ]] && strings -a "$BIN" | grep -qF "$HOME"; then
  echo "⚠ the desktop binary still contains $HOME" >&2
fi

echo ""
echo "✓ Built:"
shopt -s nullglob
for f in "$TAURI_DIR"/target/release/bundle/appimage/*.AppImage "$TAURI_DIR"/target/release/bundle/deb/*.deb; do
  ls -la "$f"
  sha256sum "$f"
done
