"""Fix the vendored skill so the secret scanner stops flagging it.

The upstream example authenticates with ``curl -u "$STRIPE_SECRET_KEY:"``.
That is a shell variable, not a credential, but the scanner's ``curl-auth-user``
rule fires on anything after ``-u`` and the secret-scan job is blocking.

The rewrite keeps the example working and uses the form Stripe documents:
an ``Authorization: Bearer`` header. It also reads better as documentation,
because the header shows WHICH credential is being sent.
"""
from __future__ import annotations

import sys
from pathlib import Path

OLD = '-u "$STRIPE_SECRET_KEY:"'
NEW = '--header "Authorization: Bearer $STRIPE_SECRET_KEY"'


def main() -> None:
    root = Path(sys.argv[1])
    changed = 0
    for sk in sorted(root.rglob("SKILL.md")):
        text = sk.read_text(encoding="utf-8")
        if OLD not in text:
            continue
        n = text.count(OLD)
        sk.write_text(text.replace(OLD, NEW), encoding="utf-8")
        changed += n
        print(f"  {sk}: {n} occurrence(s) rewritten")
    print(f"rewritten: {changed}")


if __name__ == "__main__":
    main()
