#!/usr/bin/env bash
# Run the same checks as CI, locally, before you push.
#
# The STEPS table below mirrors .github/workflows/ci.yml (job "test") and the
# gitleaks step of .github/workflows/secret-scan.yml, step by step and command
# by command. tests/test_verify_script.py fails if the two drift apart, so when
# you change a CI step, change it here too (the test tells you what differs).
# The typed-module ratchet list lives in scripts/mypy-ratchet.txt, shared by
# both, so adding a module is a one-line change in one place.
#
# Usage:
#   scripts/verify.sh [--fast] [--offline] [--strict] [--list] [-h|--help]
#
#   --fast     skip the slow parts: pytest -m "not live and not slow" without
#              coverage and without the browser/JS tests, and no whole-tree
#              mypy, pip-audit or package build. Good for a quick loop and for
#              the pre-push hook (scripts/install-hooks.sh). Run without it
#              before opening a PR.
#   --offline  skip the steps that need the network (pip-audit), and build the
#              package with the setuptools already installed instead of
#              letting pip download it (skipped when there is none).
#   --strict   a skipped optional tool (gitleaks, pip-audit) is a failure
#              instead of a warning.
#   --list     print the steps and exit.
#
# Extra pytest options (for example --ignore=tests/test_x.py) go in the usual
# PYTEST_ADDOPTS environment variable; pytest picks it up in every mode.
#
# Exit status: 0 if every blocking step passed; 1 otherwise. Advisory steps
# (continue-on-error in CI) only warn.
#
# Not run here, because they only make sense on a CI runner: checkout, Python
# setup, dependency install, the headless-Chrome browser tests (they install and
# remove Playwright) and the apt install of bubblewrap/ffmpeg. The default
# pytest run still skips whatever needs them, as it does in CI without them.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Table format: kind|CI step name|command. kind is "blocking" or "advisory";
# the command is everything after the second "|" (it may contain more).
# Order matches ci.yml. Keep commands on one line, without single quotes.
STEPS=(
  'blocking|Updater pubkey matches pinned key id — blocking|python scripts/check_updater_pubkey.py --expected-file desktop/UPDATER_KEY_ID'
  'blocking|Lint (ruff) — blocking|ruff check ops_dashboard tests'
  'advisory|Security lint (ruff S / bandit rules) — advisory|ruff check --select S --ignore S608,S110 ops_dashboard'
  'advisory|Type check (mypy) — advisory (whole tree)|mypy ops_dashboard'
  'blocking|Type check (mypy) — blocking (typed-module ratchet)|mypy --follow-imports=silent $(cat scripts/mypy-ratchet.txt)'
  'blocking|Evals (offline) — blocking|python -m ops_dashboard.evals run --report evals-report.json'
  'blocking|Dependency vulnerability scan (pip-audit) — blocking|pip-audit --strict'
  'blocking|Package builds (pip install .)|pip wheel . --no-deps --wheel-dir "$VERIFY_TMP/wheel"'
  'blocking|Test (pytest + coverage)|pytest -m "not live" --cov=ops_dashboard --cov-report=term-missing:skip-covered --cov-fail-under=60'
)

# secret-scan.yml, "Scan the full history (blocking)" (there it runs ./gitleaks).
GITLEAKS_CMD='gitleaks git --log-opts="--all" --redact --no-banner --verbose --exit-code 1 .'

# What --fast does to the table: steps it drops, and the pytest command it uses
# instead (the browser/JS tests are the slow ones; CI runs them separately).
FAST_SKIP=(
  'Type check (mypy) — advisory (whole tree)'
  'Dependency vulnerability scan (pip-audit) — blocking'
  'Package builds (pip install .)'
)
FAST_PYTEST='pytest -m "not live and not slow" --ignore-glob=tests/test_*ui_browser.py --ignore=tests/test_browser_visibility_js.py --ignore=tests/test_desktop_nav_js.py'

# Steps that need the network.
NETWORK_STEPS=('Dependency vulnerability scan (pip-audit) — blocking')

# Steps that need an optional tool, and the tool.
tool_for_step() {
  case "$1" in
    'Dependency vulnerability scan (pip-audit) — blocking') echo pip-audit ;;
    *) echo "" ;;
  esac
}

FAST=0 OFFLINE=0 STRICT=0 LIST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --fast) FAST=1 ;;
    --offline) OFFLINE=1 ;;
    --strict) STRICT=1 ;;
    --list) LIST=1 ;;
    -h|--help) sed -n '2,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "verify.sh: unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

in_list() { local x="$1"; shift; local y; for y in "$@"; do [ "$x" = "$y" ] && return 0; done; return 1; }

if [ "$LIST" = 1 ]; then
  for entry in "${STEPS[@]}"; do
    IFS='|' read -r kind name cmd <<<"$entry"
    printf '%-9s %s\n          %s\n' "$kind" "$name" "$cmd"
  done
  printf '%-9s %s\n          %s\n' "optional" "Scan the full history (gitleaks)" "$GITLEAKS_CMD"
  exit 0
fi

# Use the project virtualenv when there is one and none is active.
if [ -z "${VIRTUAL_ENV:-}" ] && [ -x .venv/bin/python ]; then
  PATH="$PWD/.venv/bin:$PATH"
  export PATH
fi

VERIFY_TMP="$(mktemp -d "${TMPDIR:-/tmp}/conductloop-verify.XXXXXX")"
export VERIFY_TMP
trap 'rm -rf "$VERIFY_TMP"' EXIT

RESULTS=()   # "status|name|seconds"
FAILED=0
WARNED=0

record() { RESULTS+=("$1|$2|$3"); }

run_step() {  # kind name cmd
  local kind="$1" name="$2" cmd="$3" start rc
  echo
  echo "━━ $name"
  echo "   \$ $cmd"
  start=$SECONDS
  bash -c "$cmd"
  rc=$?
  local secs=$((SECONDS - start))
  if [ "$rc" -eq 0 ]; then
    record PASS "$name" "$secs"
  elif [ "$kind" = advisory ]; then
    record WARN "$name (advisory, exit $rc)" "$secs"; WARNED=1
  else
    record FAIL "$name (exit $rc)" "$secs"; FAILED=1
  fi
}

skip_step() {  # name reason, optional-tool-missing => warning (failure if --strict)
  local name="$1" reason="$2" fatal="${3:-0}"
  echo
  echo "━━ $name"
  if [ "$fatal" = 1 ] && [ "$STRICT" = 1 ]; then
    echo "   ✗ $reason (--strict)"
    record FAIL "$name — $reason" 0; FAILED=1
  else
    echo "   ⚠ SKIPPED: $reason"
    record SKIP "$name — $reason" 0; WARNED=1
  fi
}

for entry in "${STEPS[@]}"; do
  IFS='|' read -r kind name cmd <<<"$entry"
  if [ "$FAST" = 1 ] && in_list "$name" "${FAST_SKIP[@]}"; then
    record SKIP "$name — --fast" 0
    continue
  fi
  if [ "$FAST" = 1 ] && [ "$name" = 'Test (pytest + coverage)' ]; then
    cmd="$FAST_PYTEST"
  fi
  if [ "$OFFLINE" = 1 ] && in_list "$name" "${NETWORK_STEPS[@]}"; then
    record SKIP "$name — --offline" 0
    continue
  fi
  if [ "$OFFLINE" = 1 ] && [ "$name" = 'Package builds (pip install .)' ]; then
    # pip's isolated build environment downloads setuptools: offline, build
    # with the one in this environment, or skip when there is none.
    if python -c 'import setuptools' >/dev/null 2>&1; then
      cmd="$cmd --no-build-isolation"
    else
      record SKIP "$name — --offline, no setuptools installed to build with" 0
      continue
    fi
  fi
  tool="$(tool_for_step "$name")"
  if [ -n "$tool" ] && ! command -v "$tool" >/dev/null 2>&1; then
    skip_step "$name" "$tool is not installed (pip install -r requirements-dev.txt)" 1
    continue
  fi
  run_step "$kind" "$name" "$cmd"
done

if command -v gitleaks >/dev/null 2>&1; then
  run_step blocking "Secret scan (gitleaks, full history)" "$GITLEAKS_CMD"
else
  skip_step "Secret scan (gitleaks, full history)" \
    "gitleaks is not installed (https://github.com/gitleaks/gitleaks#installing); CI still runs it" 1
fi

echo
echo "━━ Summary"
for r in "${RESULTS[@]}"; do
  IFS='|' read -r status name secs <<<"$r"
  case "$status" in
    PASS) icon="✓" ;;
    FAIL) icon="✗" ;;
    *) icon="⚠" ;;
  esac
  printf '  %s %-4s %s (%ss)\n' "$icon" "$status" "$name" "$secs"
done
echo
if [ "$FAILED" = 1 ]; then
  echo "✗ verify FAILED — fix the steps marked FAIL before you push."
  exit 1
fi
if [ "$WARNED" = 1 ]; then
  echo "✓ verify passed, with warnings (skipped or advisory steps above)."
else
  echo "✓ verify passed."
fi
