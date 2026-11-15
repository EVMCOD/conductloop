#!/usr/bin/env bash
# release-platforms.sh — publish the Linux and Windows builds of a ConductLoop
# release, and check that every download URL answers.
#
#   scripts/release-platforms.sh <VERSION> [--dry-run]
#
# What it does, in order:
#   1. Checks the release tag v<version> already exists in the releases repo.
#      scripts/release-desktop.sh creates it and publishes the macOS assets;
#      this script only adds the other platforms to that same release.
#   2. Dispatches the Linux and the Windows bundle workflows on main.
#   3. Waits for both runs to finish and downloads their artifacts.
#   4. Writes SHA256SUMS for the four Linux and Windows files.
#   5. Uploads them to the existing tag (gh release upload --clobber).
#   6. Rewrites the release notes with scripts/release_notes.py
#      (gh release edit --notes-file).
#   7. Checks that every download URL answers 200.
#
# What it never does: it never creates a tag and never creates a release, it
# never pushes anything, and it never signs anything (the Windows installers
# are not signed, and there is no updater on Linux or on Windows).
#
# --dry-run prints everything it would do and touches nothing: no workflow is
# dispatched, nothing is downloaded, nothing is uploaded, no release is edited
# and no URL is fetched. It needs no credentials and no network.
#
# Needs: gh CLI authenticated (read on EVMCOD/conductloop, write on
# EVMCOD/conductloop-releases), curl, python3 and the workflow files in
# .github/workflows/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$ROOT/scripts"

# Where the workflows live (they build the bundles) and where the release
# lives (the assets and the notes).
SOURCE_REPO="EVMCOD/conductloop"
RELEASE_REPO="EVMCOD/conductloop-releases"

# The workflows that build the bundles, and the artifacts they keep.
LINUX_WORKFLOW="linux-bundles.yml"
WINDOWS_WORKFLOW="windows-bundles.yml"
LINUX_APPIMAGE_ARTIFACT="conductloop-linux-appimage"
LINUX_DEB_ARTIFACT="conductloop-linux-deb"
WINDOWS_NSIS_ARTIFACT="conductloop-windows-nsis"
WINDOWS_MSI_ARTIFACT="conductloop-windows-msi"

# How long to wait for a dispatched run to show up, and how long to wait for a
# download URL to answer (GitHub needs a moment to serve a new asset).
RUN_WAIT_SECONDS="${RUN_WAIT_SECONDS:-120}"
URL_ATTEMPTS="${URL_ATTEMPTS:-10}"
URL_WAIT_SECONDS="${URL_WAIT_SECONDS:-6}"

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

die() {
  echo "✗ $*" >&2
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi
case "$1" in
  -h|--help) usage; exit 0 ;;
esac
VERSION="$1"
shift
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "✗ version '$VERSION' is not X.Y.Z (e.g. 1.2.28)" >&2
  exit 2
fi

TAG="v${VERSION}"
NOTES_SCRIPT="$SCRIPT_DIR/release_notes.py"

# The names the assets are published under. They are the names the builds
# produce and the names the published releases carry (v1.2.27).
APPIMAGE_NAME="ConductLoop_${VERSION}_amd64.AppImage"
DEB_NAME="ConductLoop_${VERSION}_amd64.deb"
SETUP_NAME="ConductLoop_${VERSION}_x64-setup.exe"
MSI_NAME="ConductLoop_${VERSION}_x64_en-US.msi"

for wf in "$LINUX_WORKFLOW" "$WINDOWS_WORKFLOW"; do
  [[ -f "$ROOT/.github/workflows/$wf" ]] || die "the workflow .github/workflows/$wf is not in this checkout"
done
[[ -f "$NOTES_SCRIPT" ]] || die "scripts/release_notes.py is missing"

echo "════════════════════════════════════════════════════════"
echo "  ConductLoop $VERSION — Linux and Windows"
echo "════════════════════════════════════════════════════════"
echo ""
echo "▸ Plan"
echo "  release:      $TAG in $RELEASE_REPO (it must already exist)"
echo "  workflows:    $LINUX_WORKFLOW, $WINDOWS_WORKFLOW (--ref main)"
echo "  artifacts:    $LINUX_APPIMAGE_ARTIFACT, $LINUX_DEB_ARTIFACT,"
echo "                $WINDOWS_NSIS_ARTIFACT, $WINDOWS_MSI_ARTIFACT"
echo "  files:        $APPIMAGE_NAME, $DEB_NAME, $SETUP_NAME, $MSI_NAME"
echo "  upload:       gh release upload $TAG --repo $RELEASE_REPO --clobber <files> SHA256SUMS"
echo "  notes:        gh release edit $TAG --repo $RELEASE_REPO --notes-file <notes>"
echo "  verify:       every download URL must answer 200"
echo ""

if [[ $DRY_RUN -eq 1 ]]; then
  echo "▸ Dry run: nothing is dispatched, downloaded, uploaded, edited or fetched."
  echo "  These are the commands that would run:"
  echo "    gh workflow run $LINUX_WORKFLOW --ref main --repo $SOURCE_REPO"
  echo "    gh workflow run $WINDOWS_WORKFLOW --ref main --repo $SOURCE_REPO"
  echo "    gh run watch <run-id> --repo $SOURCE_REPO --exit-status"
  echo "    gh run download <run-id> --repo $SOURCE_REPO -n $LINUX_APPIMAGE_ARTIFACT -D <dir>"
  echo "    gh run download <run-id> --repo $SOURCE_REPO -n $LINUX_DEB_ARTIFACT -D <dir>"
  echo "    gh run download <run-id> --repo $SOURCE_REPO -n $WINDOWS_NSIS_ARTIFACT -D <dir>"
  echo "    gh run download <run-id> --repo $SOURCE_REPO -n $WINDOWS_MSI_ARTIFACT -D <dir>"
  echo "    sha256sum <the four files> > SHA256SUMS"
  echo "    gh release upload $TAG --repo $RELEASE_REPO --clobber <files> SHA256SUMS"
  echo "    python3 scripts/release_notes.py $VERSION --out <notes>"
  echo "    gh release edit $TAG --repo $RELEASE_REPO --notes-file <notes>"
  echo "    curl -s -L -o /dev/null -w '%{http_code}' <every download URL>   (must be 200)"
  echo ""
  echo "✓ Dry run only: nothing was published."
  exit 0
fi

# ── 0. Prerequisites ─────────────────────────────────────────────────────────
command -v gh >/dev/null 2>&1 || die "gh is not installed"
command -v curl >/dev/null 2>&1 || die "curl is not installed"
command -v python3 >/dev/null 2>&1 || die "python3 is not installed"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated (run 'gh auth login')"

# The tag is the macOS release's: release-desktop.sh creates it. This script
# only adds files to it, so a missing tag is a mistake to report, not to fix.
echo "▸ Checking the release $TAG exists in $RELEASE_REPO"
if ! gh release view "$TAG" --repo "$RELEASE_REPO" >/dev/null 2>&1; then
  die "the release $TAG does not exist in $RELEASE_REPO: run scripts/release-desktop.sh $VERSION first (it creates the tag and the macOS assets)"
fi
echo "  ✓ the release $TAG exists"
echo ""

# ── 1. Dispatch the bundle workflows ─────────────────────────────────────────
echo "▸ Dispatching the bundle workflows on main"
DISPATCHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
gh workflow run "$LINUX_WORKFLOW" --ref main --repo "$SOURCE_REPO"
gh workflow run "$WINDOWS_WORKFLOW" --ref main --repo "$SOURCE_REPO"
echo "  ✓ dispatched $LINUX_WORKFLOW and $WINDOWS_WORKFLOW at $DISPATCHED_AT"
echo ""

# The id of the run that was just dispatched. `gh workflow run` does not
# print one, so the run is looked up by the moment it was created: a run of
# the same workflow that started earlier is somebody else's.
find_run() {
  local workflow="$1" since="$2" run_id=""
  local waited=0
  while [[ $waited -lt $RUN_WAIT_SECONDS ]]; do
    run_id="$(gh run list --workflow "$workflow" --repo "$SOURCE_REPO" \
      --event workflow_dispatch --branch main --limit 20 \
      --json databaseId,createdAt \
      --jq "map(select(.createdAt >= \"$since\")) | first | .databaseId // empty" 2>/dev/null || true)"
    [[ -n "$run_id" ]] && { printf '%s\n' "$run_id"; return 0; }
    sleep 2
    waited=$((waited + 2))
  done
  return 1
}

wait_for_run() {
  local workflow="$1" run_id="$2"
  echo "▸ Waiting for the $workflow run $run_id"
  if ! gh run watch "$run_id" --repo "$SOURCE_REPO" --exit-status --compact; then
    die "the $workflow run $run_id failed: nothing was uploaded (https://github.com/$SOURCE_REPO/actions/runs/$run_id)"
  fi
  echo "  ✓ the $workflow run $run_id finished"
  echo ""
}

# ── 2. Build, wait, download ─────────────────────────────────────────────────
STAGE="$(mktemp -d)"
cleanup() {
  if [[ -n "${STAGE:-}" && -d "$STAGE" ]]; then
    rm -rf "$STAGE"
  fi
}
trap cleanup EXIT

UPLOAD_DIR="$STAGE/upload"
mkdir -p "$UPLOAD_DIR"

download_artifact() {
  local run_id="$1" artifact="$2" dir="$3"
  mkdir -p "$dir"
  echo "▸ Downloading $artifact"
  gh run download "$run_id" --repo "$SOURCE_REPO" -n "$artifact" -D "$dir"
}

LINUX_RUN="$(find_run "$LINUX_WORKFLOW" "$DISPATCHED_AT")" \
  || die "the $LINUX_WORKFLOW run did not show up within ${RUN_WAIT_SECONDS}s"
WINDOWS_RUN="$(find_run "$WINDOWS_WORKFLOW" "$DISPATCHED_AT")" \
  || die "the $WINDOWS_WORKFLOW run did not show up within ${RUN_WAIT_SECONDS}s"

wait_for_run "$LINUX_WORKFLOW" "$LINUX_RUN"
wait_for_run "$WINDOWS_WORKFLOW" "$WINDOWS_RUN"

download_artifact "$LINUX_RUN" "$LINUX_APPIMAGE_ARTIFACT" "$STAGE/linux-appimage"
download_artifact "$LINUX_RUN" "$LINUX_DEB_ARTIFACT" "$STAGE/linux-deb"
download_artifact "$WINDOWS_RUN" "$WINDOWS_NSIS_ARTIFACT" "$STAGE/windows-nsis"
download_artifact "$WINDOWS_RUN" "$WINDOWS_MSI_ARTIFACT" "$STAGE/windows-msi"
echo ""

# The file the build produced. One per artifact directory; more than one
# means the runner kept something else and the wrong file could be published.
# It prints the path and returns non-zero (it is called inside a command
# substitution, where die() would only leave the subshell).
one_file() {
  local dir="$1" pattern="$2" what="$3"
  local found=()
  shopt -s nullglob
  # The glob is the point: the artifact directory holds the built file.
  # shellcheck disable=SC2206
  found=("$dir"/$pattern)
  shopt -u nullglob
  if [[ ${#found[@]} -eq 0 ]]; then
    echo "✗ the build produced no $what ($dir holds none)" >&2
    return 1
  fi
  if [[ ${#found[@]} -gt 1 ]]; then
    echo "✗ the build produced more than one $what: ${found[*]}" >&2
    return 1
  fi
  printf '%s\n' "${found[0]}"
}

# The version is in the file name. A bundle built for another version would
# be published under this release's name, which is the one mistake this
# script cannot make.
check_built_version() {
  local path="$1" base
  base="$(basename "$path")"
  [[ "$base" == *"_${VERSION}_"* ]] \
    || die "$base was not built for version $VERSION: the bundles and the release must be the same version"
}

# Copy the built file under the name the release publishes it with.
stage_artifact() {
  local src=""
  src="$(one_file "$1" "$2" "$3")" || exit 1
  check_built_version "$src"
  cp "$src" "$UPLOAD_DIR/$4"
  echo "  ✓ $(basename "$src") → $4"
}

echo "▸ Staging the files"
stage_artifact "$STAGE/linux-appimage" '*.AppImage' "AppImage" "$APPIMAGE_NAME"
stage_artifact "$STAGE/linux-deb" '*.deb' ".deb" "$DEB_NAME"
stage_artifact "$STAGE/windows-nsis" '*.exe' "NSIS installer" "$SETUP_NAME"
stage_artifact "$STAGE/windows-msi" '*.msi' ".msi" "$MSI_NAME"
echo ""

# ── 3. SHA256SUMS ────────────────────────────────────────────────────────────
# The same format `sha256sum` writes (and `sha256sum -c` reads), with the
# names of the published assets and nothing else.
sha256_of() {
  local path="$1" dir base
  dir="$(dirname "$path")"
  base="$(basename "$path")"
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "$dir" && sha256sum "$base")
  else
    (cd "$dir" && shasum -a 256 "$base")
  fi
}

echo "▸ Writing SHA256SUMS"
: > "$UPLOAD_DIR/SHA256SUMS"
for name in "$APPIMAGE_NAME" "$DEB_NAME" "$SETUP_NAME" "$MSI_NAME"; do
  sha256_of "$UPLOAD_DIR/$name" >> "$UPLOAD_DIR/SHA256SUMS"
done
cat "$UPLOAD_DIR/SHA256SUMS"
echo ""

# ── 4. Upload to the existing tag ────────────────────────────────────────────
echo "▸ Uploading to the release $TAG in $RELEASE_REPO"
gh release upload "$TAG" --repo "$RELEASE_REPO" --clobber \
  "$UPLOAD_DIR/$APPIMAGE_NAME" \
  "$UPLOAD_DIR/$DEB_NAME" \
  "$UPLOAD_DIR/$SETUP_NAME" \
  "$UPLOAD_DIR/$MSI_NAME" \
  "$UPLOAD_DIR/SHA256SUMS"
echo "  ✓ uploaded the four files and SHA256SUMS"
echo ""

# ── 5. Release notes ─────────────────────────────────────────────────────────
echo "▸ Writing the release notes"
NOTES_FILE="$STAGE/release-notes.md"
python3 "$NOTES_SCRIPT" "$VERSION" --out "$NOTES_FILE"
gh release edit "$TAG" --repo "$RELEASE_REPO" --notes-file "$NOTES_FILE"
echo "  ✓ the notes of $TAG are the ones of the changelog"
echo ""

# ── 6. Verify every download URL ─────────────────────────────────────────────
# The URLs come from the notes builder, so what is checked is exactly what
# the release page tells people to download.
echo "▸ Checking the download URLs"
while IFS= read -r url; do
  [[ -n "$url" ]] || continue
  attempt=1
  code=""
  while :; do
    code="$(curl -s -L -o /dev/null -w '%{http_code}' "$url" || true)"
    [[ "$code" == "200" ]] && break
    if [[ $attempt -ge $URL_ATTEMPTS ]]; then
      die "$url answered $code, not 200"
    fi
    sleep "$URL_WAIT_SECONDS"
    attempt=$((attempt + 1))
  done
  echo "  ✓ $url"
done < <(python3 "$NOTES_SCRIPT" "$VERSION" --urls)
echo ""

echo "════════════════════════════════════════════════════════"
echo "  ✓ ConductLoop $VERSION published for Linux and Windows"
echo "════════════════════════════════════════════════════════"
echo ""
echo "Download: https://github.com/$RELEASE_REPO/releases/tag/$TAG"
