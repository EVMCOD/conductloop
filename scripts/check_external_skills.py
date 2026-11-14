"""Check a set of external skills before they are adopted.

Run with the project venv (PyYAML required):
    .venv/bin/python scripts/check_external_skills.py <repo-skills-dir> <name> [<name> ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops_dashboard.skills import lint, parse, validate  # noqa: E402


def main() -> None:
    root = Path(sys.argv[1])
    names = sys.argv[2:]
    print(f"{'skill':34} {'desc':>5} {'body':>5}  extras")
    for name in names:
        d = root / name
        sk = d / "SKILL.md"
        if not sk.is_file():
            print(f"{name:34} FALTA")
            continue
        parsed = parse.parse_skill_file(sk)
        fm = parsed.frontmatter or {}
        desc = str(fm.get("description", ""))
        key, diags = validate.validate(fm, name, parsed.body)
        errs = [x for x in diags if x.level == "error"]
        warns = [x for x in diags if x.level != "error"]
        blocks = [f for f in lint.scan_text(sk.read_text(), name) if f.level == "block"]
        extras = sorted(p.relative_to(d).as_posix() for p in d.rglob("*")
                        if p.is_file() and p.name != "SKILL.md")
        print(f"{name:34} {len(desc):>5} {len(parsed.body.splitlines()):>5}  "
              f"errors={len(errs)} warns={len(warns)} blocks={len(blocks)} extras={extras[:4]}")
        for e in errs:
            print(f"      ERROR {e.code}: {e.message[:110]}")
        for w in warns:
            print(f"      warn  {w.code}: {w.message[:110]}")
        if fm.get("license") is None:
            print("      SIN license")
        meta = fm.get("metadata") or {}
        if not meta.get("author"):
            print(f"      metadata sin author: {meta}")


if __name__ == "__main__":
    main()
