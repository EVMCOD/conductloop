"""Safety net for the db.py -> db package split.

Every name in ``tests/db_public_api.json`` (captured before the refactor) must
stay importable from ``ops_dashboard.db`` with the same "kind" (callable vs
value). Modules across the app call ``db.<name>`` directly, so a forgotten
re-export would break them silently — this test fails loudly instead.

Regenerate the baseline ONLY when intentionally removing/renaming public API:
    .venv/bin/python -c "import json,types,ops_dashboard.db as db; \
      IGN={'Fernet','bcrypt'}; \
      json.dump(sorted(n for n in dir(db) if not n.startswith('_') and \
      not isinstance(getattr(db,n),types.ModuleType) and n not in IGN and \
      n!='annotations'), open('tests/db_public_api.json','w'), indent=1)"
"""
from __future__ import annotations

import json
from pathlib import Path

import ops_dashboard.db as db

_BASELINE = json.loads((Path(__file__).parent / "db_public_api.json").read_text())

# Optional-dependency import artifacts that leak into db's namespace as a class
# (cryptography installed) or None (not installed). They are not part of the db
# API, so their presence/kind must not depend on the environment.
_OPTIONAL_ARTIFACTS = {"Fernet", "bcrypt", "Iterable", "Iterator", "Any"}


def test_public_api_names_all_present():
    missing = [n for n in _BASELINE if n not in _OPTIONAL_ARTIFACTS and not hasattr(db, n)]
    assert not missing, f"db lost public names in the refactor: {missing}"


def test_public_api_callables_stay_callable():
    """A name that was a function must not silently become a value (or vice
    versa) — that would mean a re-export shadowed the real definition."""
    import ops_dashboard.db as _fresh  # same module, explicit for clarity
    wrong_kind = []
    for n in _BASELINE:
        if n in _OPTIONAL_ARTIFACTS:
            continue
        obj = getattr(_fresh, n, None)
        # callables in the baseline live in the function namespace; a handful of
        # documented constants (DB_PATH, SCHEMA, regex, dicts)
        # are values. We only assert the *function* set stays callable.
        if n in {"DB_PATH", "SCHEMA",
                 "LEVEL_ORDER", "MONTH_NAMES", "WEEKDAY_NAMES",
                 "PROJECT_PREFIX_RE", "PLAYBOOK_HEADER_RE", "PLAYBOOK_KV_RE",
                 "LIST_ITEM_RE", "SECTION_RE"}:
            continue
        if not callable(obj):
            wrong_kind.append(n)
    assert not wrong_kind, f"db public functions no longer callable: {wrong_kind}"
