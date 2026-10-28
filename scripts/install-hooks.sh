#!/usr/bin/env bash
# Install (or remove) the optional pre-push hook that runs scripts/verify.sh.
#
#   scripts/install-hooks.sh              install
#   scripts/install-hooks.sh --force      replace a pre-push hook that is not ours
#   scripts/install-hooks.sh --uninstall  remove it
#
# The hook is a small shim in this clone's git hooks directory that runs the
# tracked .githooks/pre-push, so updating the repo updates the hook. It does not
# set core.hooksPath: `pre-commit install` refuses to run when that is set, and
# this repository also uses pre-commit (see CONTRIBUTING.md).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
MARK="# conductloop-verify-hook"
hook="$(git rev-parse --git-path hooks/pre-push)"

case "${1:-}" in
  --uninstall)
    if [ -f "$hook" ] && grep -q "$MARK" "$hook"; then
      rm -f "$hook"
      echo "✓ removed $hook"
    else
      echo "nothing to remove (no ConductLoop pre-push hook at $hook)"
    fi
    exit 0
    ;;
  --force|"") ;;
  *) echo "usage: scripts/install-hooks.sh [--force|--uninstall]" >&2; exit 2 ;;
esac

if [ -e "$hook" ] && ! grep -q "$MARK" "$hook" && [ "${1:-}" != "--force" ]; then
  echo "✗ $hook already exists and is not ours; re-run with --force to replace it." >&2
  exit 1
fi

mkdir -p "$(dirname "$hook")"
cat > "$hook" <<SHIM
#!/bin/sh
$MARK (installed by scripts/install-hooks.sh)
exec "\$(git rev-parse --show-toplevel)/.githooks/pre-push" "\$@"
SHIM
chmod +x "$hook" .githooks/pre-push
echo "✓ pre-push hook installed at $hook"
echo "  It runs: scripts/verify.sh --fast   (change with VERIFY_ARGS, bypass with git push --no-verify)"
