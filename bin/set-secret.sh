#!/usr/bin/env bash
# Set a secret in ~/ops-dashboard/.env WITHOUT it showing on screen or landing
# in shell history. The value is read from a hidden prompt.
#
#   bash bin/set-secret.sh STRIPE_SECRET_KEY
#   bash bin/set-secret.sh STRIPE_PRICE_PRO
#
# Paste the value (e.g. copied from Notes) at the hidden prompt, press Enter.
set -euo pipefail
KEY="${1:?usage: set-secret.sh <ENV_VAR_NAME>}"
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"

printf 'Paste value for %s (input hidden), then press Enter: ' "$KEY" >&2
read -rs VALUE
echo >&2
[ -n "$VALUE" ] || { echo "empty value, aborted" >&2; exit 1; }

KEY="$KEY" VALUE="$VALUE" ENV_FILE="$ENV_FILE" python3 - <<'PY'
import os
k = os.environ["KEY"]; v = os.environ["VALUE"]; f = os.environ["ENV_FILE"]
try:
    lines = open(f).read().splitlines()
except FileNotFoundError:
    lines = []
out, found = [], False
for ln in lines:
    if ln.startswith(k + "="):
        out.append(f"{k}={v}"); found = True
    else:
        out.append(ln)
if not found:
    out.append(f"{k}={v}")
open(f, "w").write("\n".join(out) + "\n")
os.chmod(f, 0o600)
print(f"[ok] {k} saved to {f} (value not echoed)")
PY
