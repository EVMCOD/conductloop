#!/usr/bin/env bash
# Automatable Mac release checks for issue #32.
#
# Runs what CI + a Mac can prove without the maintainer's accounts, Keychain consent,
# or OAuth consoles. Prints the leftover manual checklist at the end.
#
#   scripts/mac-checks.sh              # unit + (on Darwin) sandbox-exec probes
#   scripts/mac-checks.sh --pytest-only
#
# Never touches ~/Library/Application Support/com.conductloop.desktop/ or the
# live ops.db under a checkout. For a migration check on a real DB, quit the
# app and pass a copy you made yourself:
#
#   sqlite3 "$HOME/Library/Application Support/com.conductloop.desktop/data/ops.db" \
#     ".backup /tmp/ops-copy.db"
#   CONDUCTLOOP_DB_COPY=/tmp/ops-copy.db scripts/mac-checks.sh
#
# Needs: bash, python3, a repo venv (or OPS_PYTHON). On macOS, sandbox-exec.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${OPS_PYTHON:-}"
if [[ -z "$PY" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PY="$ROOT/.venv/bin/python"
  else
    PY="$(command -v python3)"
  fi
fi
PORT="${CONDUCTLOOP_PORT:-7777}"
PYTEST_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --pytest-only) PYTEST_ONLY=1 ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

failures=0
ok()  { echo "✓ $1"; }
bad() { echo "✗ $1"; failures=$((failures + 1)); }

echo "== issue #32 mac-checks (automatable) =="
echo "python: $PY"
echo "repo:   $ROOT"

# ── port 7777 ───────────────────────────────────────────────────────────────
if curl -s -m 1 -o /dev/null "http://127.0.0.1:${PORT}/api/health" 2>/dev/null; then
  bad "something already answers on :${PORT} (make serve / another ConductLoop). Stop it before a packaged-app start-up check; unit tests use ephemeral ports and are fine."
else
  ok "port ${PORT} is free (packaged-app start-up will not collide with make serve)"
fi

# ── pytest: issue #32 automation + related guards already on main ───────────
export HF_HUB_OFFLINE=1
TESTS=(
  tests/test_issue32_automation.py
  tests/test_app_token_guard.py
  tests/test_macos_minimum.py
  tests/test_session_boot_token.py
  tests/test_health_instance.py
)
echo
echo "-- pytest (${#TESTS[@]} files) --"
if ! "$PY" -m pytest -m "not live" -q --tb=line "${TESTS[@]}"; then
  bad "pytest failed (see above)"
else
  ok "pytest: app-token guard, boot cookie, macOS minimum sync, migration 20–24, sandbox"
fi

# ── optional: migration check on a user-supplied DB *copy* ──────────────────
if [[ -n "${CONDUCTLOOP_DB_COPY:-}" ]]; then
  echo
  echo "-- migrations on CONDUCTLOOP_DB_COPY --"
  if [[ ! -f "$CONDUCTLOOP_DB_COPY" ]]; then
    bad "CONDUCTLOOP_DB_COPY is not a file: $CONDUCTLOOP_DB_COPY"
  elif [[ "$CONDUCTLOOP_DB_COPY" == *"/Application Support/com.conductloop.desktop/"* ]]; then
    bad "refusing to open the live Application Support DB; pass a sqlite3 .backup copy under /tmp"
  else
    if CONDUCTLOOP_DB_COPY="$CONDUCTLOOP_DB_COPY" "$PY" - <<'PY'
import os, sqlite3, sys
from ops_dashboard import migrations
path = os.environ["CONDUCTLOOP_DB_COPY"]
conn = sqlite3.connect(path)
conn.execute("PRAGMA foreign_keys = ON")
before = conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0]
applied = migrations.run_migrations(conn)
conn.commit()
assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
after = conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0]
assert before == after, (before, after)
cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_sessions)")]
assert "browser_mode" in cols
expected = {v: d for v, d, _ in migrations.MIGRATIONS if 20 <= v <= 24}
rows = {int(v): d for v, d in conn.execute(
    "SELECT version, description FROM schema_migrations WHERE version BETWEEN 20 AND 24")}
assert rows == expected, (rows, expected)
assert migrations.run_migrations(conn) == []
print(f"applied={applied!r} agent_sessions={after} integrity=ok")
conn.close()
PY
    then
      ok "migrations on copy: integrity ok, agent_sessions unchanged, descriptions 20–24 match"
    else
      bad "migrations on CONDUCTLOOP_DB_COPY failed"
    fi
  fi
fi

if [[ "$PYTEST_ONLY" -eq 1 ]]; then
  echo
  [[ "$failures" -eq 0 ]] && exit 0 || exit 1
fi

# ── Darwin-only live probes (also covered by pytest on macOS) ───────────────
if [[ "$(uname -s)" == "Darwin" ]]; then
  echo
  echo "-- sandbox-exec detect_backend --"
  if "$PY" - <<'PY'
from ops_dashboard.sessions import sandbox
sandbox._PROBED.clear()
assert sandbox.detect_backend() == "sandbox-exec", sandbox.detect_backend()
print("detect_backend = sandbox-exec")
PY
  then
    ok "detect_backend() → sandbox-exec"
  else
    bad "detect_backend() is not sandbox-exec"
  fi
else
  echo
  echo "(not Darwin: skipping live sandbox-exec probe; CI covers Linux bwrap)"
fi

# ── leftover manual checklist ───────────────────────────────────────────────
cat <<'MANUAL'

== still manual on a Mac (issue #32) — needs the maintainer ==
[ ] Packaged-app start-up through /__cl/boot (WKWebView + cl_app cookie); port 7777 free
[ ] Safari on http://localhost:7777/ while the app is open → 403 (note if Safari omits Sec-Fetch-*)
[ ] BYOK Keychain round-trip (save / no prompt after restart / delete removes item) — no secrets in the issue
[ ] Connectors with his OAuth apps / tokens (Google, Microsoft 365, Slack, GitHub, Linear, Todoist, Readwise, RSS, Notion token)
[ ] Live evals L2 — blocked on #35
[ ] Decision: minimum macOS is already 13.0 in tauri.conf + test_macos_minimum.py; confirm or leave as-is (do not raise to 14 without updater check)
[ ] Optional: LSMinimumSystemVersion in a built .app and nm weak Network.framework (needs a local debug/release build)

Already automated (this script + CI): local API guard headers, boot nonce/cookie,
migration 20–24 on a synthetic/backup copy, browser URL policy (cargo test url-policy),
macOS minimum sync across docs/config, sandbox-exec detect_backend + fail-closed +
network/write/ssh probes on Darwin.

MANUAL

echo
if [[ "$failures" -eq 0 ]]; then
  ok "mac-checks finished clean"
  exit 0
fi
bad "mac-checks finished with $failures failure(s)"
exit 1
