"""Setup readiness checklist — the 10 things to configure for a healthy
ConductLoop, each with a live pass/fail and how-to-fix. Powers the /setup
tutorial page and `ops setup`.

Every check is defensive: one failing check reports ``ok: False`` with a detail
instead of breaking the list.
"""
from __future__ import annotations

from datetime import UTC

from . import db


def _safe(fn):
    try:
        return fn()
    except Exception as e:  # never let one check break the page
        return False, f"check errored: {type(e).__name__}: {e}"


def _vault():
    from pathlib import Path
    root = db.vault_root()
    ok = Path(root).exists()
    return ok, (f"vault at {root}" if ok else f"not found: {root} — choose your folder on the welcome page or in Setup → Library")


def _database():
    from . import migrations
    with db.get_connection() as conn:
        cur = migrations.current_version(conn)
    target = max([m[0] for m in migrations.MIGRATIONS] + [migrations.BASELINE])
    ok = cur >= target
    return ok, f"schema v{cur} (latest v{target})"


def _provider():
    from . import brain
    p = brain.resolve_provider()
    ok = getattr(p, "name", "mock") != "mock"
    if ok:
        return True, f"provider: {p.name}"
    # No environment variables and no files to edit: the key goes in the app.
    return False, "no AI provider yet — add your key in Credentials"


def _embeddings():
    try:
        import sentence_transformers  # noqa: F401
        return True, "sentence-transformers installed (semantic search on)"
    except Exception:
        import sys
        if getattr(sys, "frozen", False):  # the desktop app ships without it
            return False, "not in the desktop app: search matches words, not meaning"
        return False, "pip install sentence-transformers for semantic search"


def _secrets():
    from . import secrets_store
    probe = "__setup_probe__"
    secrets_store.set_secret(probe, "ok")
    got = secrets_store.get_secret(probe)
    if got != "ok":
        return False, "secret store not writable"
    try:
        from . import secret_vault

        where = secret_vault.backend()
    except Exception:
        where = "keychain"
    detail = ("the keychain of this Mac" if where == "keychain"
              else "an encrypted file (this machine has no keychain)")
    return True, f"secret store read/write OK — the keys live in {detail}"


def _logging():
    import os

    from . import logging_setup
    d = logging_setup._log_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        ok = os.access(d, os.W_OK)
    except Exception:
        ok = False
    return ok, (f"logs at {d}" if ok else f"log dir not writable: {d}")


def _projects():
    from . import projects
    n = len(projects.PROJECTS)
    if n:
        return True, f"{n} project(s) configured"
    from .app_paths import path as _app_path
    return False, ("none — projects group notes, repos and URLs by business; "
                   f"they are defined in {_app_path('projects.local.json')}")


def _documents():
    with db.get_connection() as conn:
        n = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    return n > 0, (f"{n} documents indexed" if n else "nothing indexed yet — press “Index now” in Setup → Library")


def _obsidian():
    from . import obsidian_sync
    s = obsidian_sync.status()
    ok = s["folder_exists"]
    return ok, (f"{s['note_count']} memory notes in vault" if ok
                else "not exported yet — press Export under “Obsidian memory” below")


# (id, label, fn) — order = the tutorial's steps 1..9. (The 'edition' step was
# removed on 2026-10-21: ConductLoop is free, there is nothing to resolve.)
# Extras someone who only brings their notes does without: shown, never
# counted against "ready".
OPTIONAL = frozenset({"embeddings", "projects", "obsidian_sync"})
_CHECKS = [
    ("vault", "Folder for ConductLoop's own notes", _vault),
    ("database", "Database initialized + migrated", _database),
    ("provider", "AI provider (BYOK) configured", _provider),
    ("embeddings", "Semantic-search embeddings available", _embeddings),
    ("secrets", "Secret store (keychain) working", _secrets),
    ("logging", "Logging directory writable", _logging),
    ("projects", "At least one project defined", _projects),
    ("documents", "Content indexed", _documents),
    ("obsidian_sync", "Obsidian memory sync set up", _obsidian),
]


def checklist() -> dict:
    """Run all onboarding checks. Returns {ready, passed, total, required,
    required_passed, checks:[...]}; ``ready`` looks at the required ones only."""
    items = []
    for step, (cid, label, fn) in enumerate(_CHECKS, start=1):
        ok, detail = _safe(fn)
        items.append({"step": step, "id": cid, "label": label, "ok": bool(ok), "detail": detail,
                      "optional": cid in OPTIONAL})
    passed = sum(1 for i in items if i["ok"])
    required = [i for i in items if not i["optional"]]
    required_passed = sum(1 for i in required if i["ok"])
    return {"ready": required_passed == len(required), "passed": passed, "total": len(items),
            "required": len(required), "required_passed": required_passed, "checks": items}


# --- first run -------------------------------------------------------------
# The welcome page is for someone who has just installed the app. "Just
# installed" is decided from what is on the machine, never from a version
# number, so nobody who upgrades sees it: any notes indexed, any key, any folder
# chosen, or a vault set from the environment means they are already set up.


def _marker() -> dict:
    from . import brain
    m = brain._load_config().get("onboarding")
    return m if isinstance(m, dict) else {}


def _has_key() -> bool:
    """Is any AI key set up? Looks at the references in config.json and at the
    environment, never reads a secret and never probes the network."""
    import os

    from . import brain, byok_keys
    cfg = brain._load_config()
    return any(
        os.environ.get(byok_keys.ENV_VAR[p]) or cfg.get(byok_keys.REF_FIELD[p]) or cfg.get(byok_keys.PLAINTEXT_FIELD[p])
        for p in byok_keys.PROVIDERS
    )


def state() -> dict:
    """What the welcome page needs: {fresh, dismissed, completed, vault_source,
    vault_exists, documents, has_key}. Never contains a key or any secret."""
    from . import library

    marker = _marker()
    dismissed = bool(marker.get("dismissed_at"))
    completed = bool(marker.get("completed_at"))
    try:
        with db.get_connection() as conn:
            documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    except Exception:
        documents = 0
    has_key = _has_key()
    source = db.vault_root_source()
    from . import brain
    # Any setting already in config.json (a provider, a folder, a vault...) means
    # this is not a first run; the marker of this very page does not count.
    settings = [k for k in brain._load_config() if k != "onboarding"]
    configured = source != "default" or bool(library.index_folders()) or bool(settings)
    fresh = not (dismissed or completed or documents or has_key or configured)
    return {
        "fresh": fresh,
        "dismissed": dismissed,
        "completed": completed,
        "vault_source": source,
        "vault_exists": db.vault_root().is_dir(),
        "documents": int(documents),
        "has_key": bool(has_key),
    }


def mark(kind: str) -> None:
    """Remember that the welcome page was finished ("completed") or left
    ("dismissed"), so it is not shown again."""
    from datetime import datetime

    from . import brain
    if kind not in ("completed", "dismissed"):
        raise ValueError(kind)
    cfg = brain._load_config()
    current = cfg.get("onboarding")
    marker: dict = current if isinstance(current, dict) else {}
    marker[f"{kind}_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    cfg["onboarding"] = marker
    brain._save_config(cfg)
