"""Setup readiness checklist — the 10 things to configure for a healthy
ConductLoop, each with a live pass/fail and how-to-fix. Powers the /setup
tutorial page and `ops setup`.

Every check is defensive: one failing check reports ``ok: False`` with a detail
instead of breaking the list.
"""
from __future__ import annotations

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
    return ok, (f"vault at {root}" if ok else f"not found: {root}")


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
    return ok, (f"provider: {p.name}" if ok else "no AI provider — set BRAIN_PROVIDER + a key (or run Ollama)")


def _embeddings():
    try:
        import sentence_transformers  # noqa: F401
        return True, "sentence-transformers installed (semantic search on)"
    except Exception:
        return False, "optional: pip install sentence-transformers for semantic search"


def _secrets():
    from . import secrets_store
    probe = "__setup_probe__"
    secrets_store.set_secret(probe, "ok")
    got = secrets_store.get_secret(probe)
    return got == "ok", ("secret store read/write OK" if got == "ok" else "secret store not writable")


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
    return n > 0, (f"{n} project(s) configured" if n else "no projects — add projects.local.json or OPS_PROJECTS_FILE")


def _documents():
    with db.get_connection() as conn:
        n = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    return n > 0, (f"{n} documents indexed" if n else "nothing indexed yet — run `make index`")


def _obsidian():
    from . import obsidian_sync
    s = obsidian_sync.status()
    ok = s["folder_exists"]
    return ok, (f"{s['note_count']} memory notes in vault" if ok
                else "not synced yet — run `ops obsidian-sync`")


# (id, label, fn) — order = the tutorial's steps 1..9. (The 'edition' step was
# removed on 2026-10-21: ConductLoop is free, there is nothing to resolve.)
_CHECKS = [
    ("vault", "Obsidian vault configured", _vault),
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
    """Run all onboarding checks. Returns {ready, passed, total, checks:[...]}."""
    items = []
    for step, (cid, label, fn) in enumerate(_CHECKS, start=1):
        ok, detail = _safe(fn)
        items.append({"step": step, "id": cid, "label": label, "ok": bool(ok), "detail": detail})
    passed = sum(1 for i in items if i["ok"])
    return {"ready": passed == len(items), "passed": passed, "total": len(items), "checks": items}
