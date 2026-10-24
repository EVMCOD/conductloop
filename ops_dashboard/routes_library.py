"""Library settings (/setup): which folders are indexed, and an "index now".

Local only, like the other settings routes: these are paths on this Mac.
"""
from __future__ import annotations

import threading

from . import library
from .routes_learning import _body, _err, _local_only, _send_json
from .routing import router

_running = threading.Lock()


@router.get("/api/library")
@_local_only
def library_status(handler) -> None:
    _send_json(handler, 200, {"ok": True, "indexing": _running.locked(), **library.status()})


@router.post("/api/library")
@_local_only
def library_save(handler) -> None:
    data = _body(handler)
    if data is None:
        return
    folders = data.get("index_folders")
    workspace = data.get("memory_workspace")
    if folders is not None and not (isinstance(folders, list) and all(isinstance(f, str) for f in folders)):
        _err(handler, 400, "bad_request", "index_folders must be a list of paths")
        return
    if workspace is not None and not isinstance(workspace, str):
        _err(handler, 400, "bad_request", "memory_workspace must be a path")
        return
    map_folder = data.get("map_export_folder")
    if map_folder is not None and not isinstance(map_folder, str):
        _err(handler, 400, "bad_request", "map_export_folder must be a path")
        return
    try:
        saved = library.save_settings(index_folders=folders, memory_workspace=workspace,
                                      map_export_folder=map_folder)
    except ValueError as e:
        _err(handler, 400, "bad_request", str(e))
        return
    _send_json(handler, 200, {"ok": True, **saved})


@router.post("/api/library/index")
@_local_only
def library_index(handler) -> None:
    """Start an incremental re-index in the background (one at a time)."""
    if not _running.acquire(blocking=False):
        _send_json(handler, 200, {"ok": True, "started": False, "indexing": True})
        return

    def work() -> None:
        try:
            library.refresh()
        finally:
            _running.release()

    threading.Thread(target=work, name="library-index", daemon=True).start()
    _send_json(handler, 202, {"ok": True, "started": True, "indexing": True})


@router.get("/api/library/structure")
@_local_only
def library_structure(handler) -> None:
    """The map of the work: projects, ideas, and the projects still unnamed."""
    from . import notes_structure

    try:
        data = notes_structure.structure()
    except Exception as e:
        _err(handler, 500, "structure_failed", str(e))
        return
    _send_json(handler, 200, {"ok": True, **data})


@router.get("/api/library/proposal")
@_local_only
def library_proposal(handler) -> None:
    """The lines that would put the missing projects back in the index.

    The index is written by hand, so it goes out of date. This proposes the
    lines that are missing, in the format the index already uses. Nothing is
    written: the index is the user's prose and the choice of what to paste
    is theirs.
    """
    from . import notes_structure

    try:
        data = notes_structure.propose_index_lines()
    except Exception as e:
        _err(handler, 500, "proposal_failed", str(e))
        return
    _send_json(handler, 200, {"ok": True, **data})


@router.post("/api/library/apply")
@_local_only
def library_apply(handler) -> None:
    """Put the chosen projects back in the index.

    The index is the user's prose: the product adds the lines it was told
    to add, at the end of the section they belong to, and updates the
    counts in the header. Nothing else changes, and there is a copy before
    the change.

    The default is a dry run: the answer says what would change, so the
    user sees it before it happens.
    """
    from . import notes_structure

    data = _body(handler) or {}
    stems = data.get("stems")
    if stems is not None and not isinstance(stems, list):
        _err(handler, 400, "bad_request", "stems must be a list")
        return
    for entry in (stems or []):
        if not isinstance(entry, (str, dict)):
            _err(handler, 400, "bad_request", "each entry must be a name or {stem, section}")
            return
    dry_run = bool(data.get("dry_run", True))
    try:
        result = notes_structure.apply_index_lines(
            [str(s) for s in (stems or [])], dry_run=dry_run)
    except Exception as e:
        _err(handler, 500, "apply_failed", str(e))
        return
    if not result.get("ok"):
        _err(handler, 400, "bad_request", str(result.get("error") or "failed"))
        return
    _send_json(handler, 200, {"ok": True, **result})


@router.post("/api/library/link-orphans")
@_local_only
def library_link_orphans(handler) -> None:
    """Link the notes of a project that its page forgot.

    The notes are in the project's own folder and they keep naming it, but
    the page does not link them: they are in the vault and in nobody's view.
    """
    from . import notes_structure

    data = _body(handler) or {}
    project = str(data.get("project") or "").strip()
    stems = data.get("stems")
    if not project:
        _err(handler, 400, "bad_request", "project is required")
        return
    if stems is not None and not isinstance(stems, list):
        _err(handler, 400, "bad_request", "stems must be a list")
        return
    dry_run = bool(data.get("dry_run", True))
    try:
        result = notes_structure.apply_orphan_links(
            project, [str(s) for s in (stems or [])], dry_run=dry_run)
    except Exception as e:
        _err(handler, 500, "link_failed", str(e))
        return
    if not result.get("ok"):
        _err(handler, 400, "bad_request", str(result.get("error") or "failed"))
        return
    _send_json(handler, 200, {"ok": True, **result})


@router.post("/api/library/undo")
@_local_only
def library_undo(handler) -> None:
    """Take back the lines the product added to the index."""
    from . import notes_structure

    try:
        result = notes_structure.undo_index_lines()
    except Exception as e:
        _err(handler, 500, "undo_failed", str(e))
        return
    if not result.get("ok"):
        _err(handler, 400, "bad_request", str(result.get("error") or "failed"))
        return
    _send_json(handler, 200, {"ok": True, **result})


@router.get("/api/library/pages")
@_local_only
def library_pages(handler) -> None:
    """The map as pages: one per project, linked the way a vault follows.

    The flat map is a file; these are a graph. An agent that needs one
    project reads one page instead of 39 KB, and Obsidian can follow the
    links in both directions.
    """
    from . import notes_structure, vault_pages

    try:
        data = notes_structure.structure()
    except Exception as e:
        _err(handler, 500, "structure_failed", str(e))
        return

    destinations = library.map_export_destinations()
    folders: list[dict] = []
    for destination in destinations:
        pages = destination / vault_pages.PAGES_DIRNAME
        index = pages / vault_pages.INDEX_NAME
        folders.append({
            "path": str(pages),
            "index": str(index) if index.exists() else "",
            "written": len(sorted(pages.glob("*.md"))) if pages.is_dir() else 0,
            "areas": len(sorted((pages / vault_pages.AREAS_DIRNAME).glob("*.md")))
            if (pages / vault_pages.AREAS_DIRNAME).is_dir() else 0,
        })
    _send_json(handler, 200, {"ok": True, "folders": folders,
                               "projects": len([p for p in data["projects"] if p.get("notes")]),
                               "areas": len(data.get("ideas") or [])})


@router.post("/api/library/export")
@_local_only
def library_export(handler) -> None:
    """Write the map where the next agent will find it.

    The point of a second brain is that it does not matter which agent is
    looking at it: the map is a file in the agent's workspace, not a memory
    one agent happens to have. It is also written where the notes live, so
    it travels with them.
    """
    from . import library, notes_structure

    try:
        written = notes_structure.write_agent_map(
            also_to=library.map_export_destinations())
    except Exception as e:
        _err(handler, 500, "export_failed", str(e))
        return
    _send_json(handler, 200, {"ok": True, **written})
