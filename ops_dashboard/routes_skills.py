"""Skills routes for the "Aprendizaje" page (PR L5). Local-only, like the
rest of /api/learning (registered by importing this module from
routes_learning). Nothing here fetches anything remote (D16)."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from .routes_learning import _body, _err, _local_only, _send_json
from .routing import router
from .skills import catalog


def _reindex() -> dict:
    from .indexers import skills as skills_indexer
    return skills_indexer.run()


def _state() -> dict:
    return {
        "ok": True,
        "external_dirs_enabled": catalog.external_dirs_enabled(),
        "dirs": {"vault": str(catalog.vault_dir()), "agents_dir": str(catalog.agents_dir()),
                 "claude_dir": str(catalog.claude_dir())},
        "skills": catalog.list_indexed(),
        "trusted_repos": catalog.trusted_repos(),
    }


def _fail(handler, e: catalog.SkillError) -> None:
    _err(handler, e.status, e.code, e.message)


@router.get("/api/learning/skills")
@_local_only
def skills_list(handler) -> None:
    _send_json(handler, 200, _state())


@router.post("/api/learning/skills/reindex")
@_local_only
def skills_reindex(handler) -> None:
    if _body(handler) is None:
        return
    counters = _reindex()
    _send_json(handler, 200, {**_state(), "counters": counters})


@router.post("/api/learning/skills/settings")
@_local_only
def skills_settings(handler) -> None:
    """Body: {external_dirs: bool} — D2 opt-in for ~/.agents/skills + ~/.claude/skills."""
    data = _body(handler)
    if data is None:
        return
    v = data.get("external_dirs")
    if not isinstance(v, bool):
        _err(handler, 400, "bad_setting", "external_dirs must be true or false")
        return
    catalog.set_external_dirs_enabled(v)
    counters = _reindex()
    _send_json(handler, 200, {**_state(), "counters": counters})


@router.post("/api/learning/skills/enable")
@_local_only
def skills_enable(handler) -> None:
    data = _body(handler)
    if data is None:
        return
    key, enabled = data.get("key"), data.get("enabled")
    if not isinstance(key, str) or not isinstance(enabled, bool):
        _err(handler, 400, "bad_request", "key (string) and enabled (bool) are required")
        return
    try:
        catalog.set_enabled(key, enabled)
    except catalog.SkillError as e:
        _fail(handler, e)
        return
    _send_json(handler, 200, {"ok": True, "key": key, "enabled": enabled})


@router.post("/api/learning/skills/export")
@_local_only
def skills_export(handler) -> None:
    """D1: copy a <vault>/skills skill to ~/.agents/skills/<name> (never overwrites)."""
    data = _body(handler)
    if data is None:
        return
    key = data.get("key")
    if not isinstance(key, str):
        _err(handler, 400, "bad_request", "key is required")
        return
    try:
        dest = catalog.export_skill(key)
    except catalog.SkillError as e:
        _fail(handler, e)
        return
    _send_json(handler, 200, {"ok": True, "path": str(dest)})


@router.post("/api/learning/skills/trust")
@_local_only
def skills_trust(handler) -> None:
    """Body: {repo_path, trusted: bool} — D13, project skills per repo."""
    data = _body(handler)
    if data is None:
        return
    trusted = data.get("trusted")
    if not isinstance(trusted, bool):
        _err(handler, 400, "bad_request", "trusted must be true or false")
        return
    try:
        path = catalog.set_repo_trust(data.get("repo_path"), trusted)
    except catalog.SkillError as e:
        _fail(handler, e)
        return
    _send_json(handler, 200, {"ok": True, "repo_path": path, "trusted": trusted,
                              "trusted_repos": catalog.trusted_repos()})


@router.get("/api/learning/skills/project")
@_local_only
def skills_project(handler) -> None:
    repo = (parse_qs(urlparse(handler.path).query).get("repo") or [""])[0]
    if not repo:
        _err(handler, 400, "bad_repo", "repo is required")
        return
    trusted = catalog.is_trusted(repo)
    recs = catalog.project_skills(repo)
    _send_json(handler, 200, {"ok": True, "repo": repo, "trusted": trusted, "skills": [
        {"key": r.key, "description": r.description, "path": r.path, "loadable": r.loadable,
         "diagnostics": r.diag_dicts()} for r in recs]})
