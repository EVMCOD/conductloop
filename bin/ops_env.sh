#!/bin/bash
# Shared environment for the scheduled jobs (launchd on macOS).
#
# Every job script sources this file: bin/run_recap_cron.sh, bin/run_digest.sh,
# bin/poll_ga.sh, bin/rotate-logs.sh, bin/poll_revenue.sh, bin/check_urls.sh and
# scripts/poll_analytics.sh.
#
# It exists because of a real incident. The jobs used to `cd $HOME/ops-dashboard`
# and run `python -m ops_dashboard.cli`, so Python imported whatever
# ops_dashboard/ sat in that folder — a stale copy of the tree, twelve releases
# behind — and the daily Google Analytics / Search Console poll died on
# "No module named 'google.analytics'" every single day without anybody
# noticing. The fix was already in the repository; the job simply never ran it.
#
# What this file guarantees:
#   * the code that runs is THIS checkout, whatever the current directory;
#   * the data it writes is the live database, named explicitly;
#   * the interpreter is this checkout's virtualenv;
#   * the logs land where the previous logs already are.
#
# Machine-specific settings live in scripts/ops.local.env (gitignored); see
# scripts/ops.local.env.example. Every value can also come from the
# environment, which is what the launchd plists do.

# The repository root: the folder that holds bin/, scripts/ and ops_dashboard/.
OPS_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export OPS_REPO_ROOT

# Local overrides first, so they win over every default below.
if [ -f "$OPS_REPO_ROOT/scripts/ops.local.env" ]; then
  # shellcheck disable=SC1091
  . "$OPS_REPO_ROOT/scripts/ops.local.env"
fi

# The overrides the local file may set are read by Python through os.environ,
# so they have to be exported, not just set in the shell.
for _ops_var in OPS_PYTHON OPS_DATA_DIR OPS_LOG_DIR OPS_CONFIG_PATH \
                OPS_SECRETS_DIR OPS_DISABLE_KEYCHAIN; do
  if [ -n "${!_ops_var:-}" ]; then
    # shellcheck disable=SC2163  # the name is in a variable on purpose
    export "$_ops_var"
  fi
done
unset _ops_var

# 1. The interpreter: this checkout's virtualenv, else the legacy one, else PATH.
if [ -z "${OPS_PYTHON:-}" ]; then
  for candidate in "$OPS_REPO_ROOT/.venv/bin/python" "$HOME/ops-dashboard/.venv/bin/python"; do
    if [ -x "$candidate" ]; then
      OPS_PYTHON="$candidate"
      break
    fi
  done
fi
if [ -z "${OPS_PYTHON:-}" ]; then
  OPS_PYTHON="$(command -v python3 || command -v python || true)"
fi
export OPS_PYTHON

# 2. The code: this checkout, ahead of anything else on sys.path. The current
#    directory still wins over PYTHONPATH, so the job scripts must not cd into
#    another checkout — that is what broke the poll in the first place.
export PYTHONPATH="$OPS_REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

# 3. The data: the live database, said out loud. Without this, app_paths picks
#    <repo>/data whenever that folder exists, and the job would write into a
#    checkout instead of the database the desktop app reads.
if [ -z "${OPS_DATA_DIR:-}" ]; then
  if [ -f "$OPS_REPO_ROOT/data/ops.db" ]; then
    OPS_DATA_DIR="$OPS_REPO_ROOT/data"
  elif [ -f "$HOME/ops-dashboard/data/ops.db" ]; then
    OPS_DATA_DIR="$HOME/ops-dashboard/data"
  fi
fi
if [ -n "${OPS_DATA_DIR:-}" ]; then
  export OPS_DATA_DIR
fi

# 4. The logs: where the previous logs already are.
if [ -z "${OPS_LOG_DIR:-}" ]; then
  if [ -d "$HOME/ops-dashboard/logs" ]; then
    OPS_LOG_DIR="$HOME/ops-dashboard/logs"
  else
    OPS_LOG_DIR="$OPS_REPO_ROOT/logs"
  fi
fi
export OPS_LOG_DIR
mkdir -p "$OPS_LOG_DIR"

# 5. The working directory: this checkout. Python puts the current directory
#    first on sys.path, ahead of PYTHONPATH, so a job that stays in another
#    checkout keeps importing that checkout's package — that is the trap this
#    file exists to close. Moving here is what fixes it; PYTHONPATH above is
#    the second line of defence.
cd "$OPS_REPO_ROOT" || {
  echo "[ops_env] cannot enter $OPS_REPO_ROOT" >&2
  exit 1
}

# Fails loudly when the code that is about to run is not this checkout, or when
# the package cannot be imported at all. A job that quietly runs the wrong
# version of the app is worse than a job that fails.
ops_check_code() {
  "$OPS_PYTHON" - <<'PY'
import os
import pathlib
import sys

try:
    import ops_dashboard
except Exception as exc:  # noqa: BLE001 - the point is to report anything
    print(f"[ops_env] cannot import ops_dashboard: {exc}", file=sys.stderr)
    raise SystemExit(1)

root = pathlib.Path(os.environ["OPS_REPO_ROOT"]).resolve()
loaded = pathlib.Path(ops_dashboard.__file__).resolve().parent.parent
if loaded != root:
    print(
        f"[ops_env] WRONG CODE: the job would run {loaded}, not {root}",
        file=sys.stderr,
    )
    raise SystemExit(1)
print(f"[ops_env] code ok: {loaded} (v{ops_dashboard.__version__})")
PY
}
