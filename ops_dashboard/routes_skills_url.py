"""Local-only routes for importing a published skill by URL (gap 3, step 2).

Kept in its own module so ``routes_skills.py`` stays free of any network
client (D16: the catalog and the local-folder importer never fetch).
"""
from __future__ import annotations

from .routes_learning import _body, _err, _local_only, _send_json
from .routing import router
from .skills import catalog
from .skills_url_import import approve, discard, preview_from_url


def _reindex() -> dict:
    from .indexers import skills as skills_indexer
    return skills_indexer.run()


def _fail(handler, e: catalog.SkillError) -> None:
    _err(handler, e.status, e.code, e.message)


@router.post("/api/learning/skills/import-url")
@_local_only
def skills_import_url_preview(handler) -> None:
    """Body: {url}. Fetch into a sandbox and return a review payload."""
    data = _body(handler)
    if data is None:
        return
    try:
        result = preview_from_url(data.get("url"))
    except catalog.SkillError as e:
        _fail(handler, e)
        return
    _send_json(handler, 200, result)


@router.post("/api/learning/skills/import-url/approve")
@_local_only
def skills_import_url_approve(handler) -> None:
    """Body: {id, hash}. Copy the reviewed staging folder into the vault."""
    data = _body(handler)
    if data is None:
        return
    try:
        result = approve(data.get("id"), data.get("hash"))
    except catalog.SkillError as e:
        _fail(handler, e)
        return
    from .routes_skills import _state
    counters = _reindex()
    _send_json(handler, 200, {**_state(), **result, "counters": counters})


@router.post("/api/learning/skills/import-url/discard")
@_local_only
def skills_import_url_discard(handler) -> None:
    """Body: {id}. Delete the staging folder without writing to the vault."""
    data = _body(handler)
    if data is None:
        return
    try:
        result = discard(data.get("id"))
    except catalog.SkillError as e:
        _fail(handler, e)
        return
    _send_json(handler, 200, result)
