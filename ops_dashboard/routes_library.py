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
