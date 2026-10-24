"""Where the library comes from, and keeping its index fresh.

``db.vault_root()`` is the vault ConductLoop *writes* to (daily briefing, agent
notes, memory export). People also want it to *read* folders it must not write
to — their own Obsidian vault, an agent workspace. Those live in config.json,
because the desktop app has no .env:

  "index_folders":    ["~/Documents/Obsidian/MyVault", ...]   indexed as notes
  "memory_workspace": "~/agent-workspace"                     MEMORY.md, memory/*.md

$OPS_EXTRA_VAULTS (os.pathsep-separated) and $OPS_WORKSPACE still win.

Without a periodic re-index the library froze at the last manual run: one
install answered from four-month-old notes. ``refresh()`` is incremental
(unchanged files are skipped), so the scheduler runs it every hour.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import app_paths, db

MAX_FOLDERS = 20


def _config() -> dict:
    try:
        return json.loads(app_paths.config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def index_folders() -> list[Path]:
    """Extra read-only note folders that exist, without the write vault or duplicates."""
    env = os.environ.get("OPS_EXTRA_VAULTS")
    raw = env.split(os.pathsep) if env else (_config().get("index_folders") or [])
    seen = {db.vault_root().expanduser().resolve()}
    out: list[Path] = []
    for item in raw[:MAX_FOLDERS]:
        if not isinstance(item, str) or not item.strip():
            continue
        path = Path(item.strip()).expanduser()
        if not path.is_dir() or path.resolve() in seen:
            continue
        seen.add(path.resolve())
        out.append(path)
    return out


def memory_workspace() -> Path:
    raw = os.environ.get("OPS_WORKSPACE") or _config().get("memory_workspace") or str(Path.home() / ".ops-workspace")
    return Path(raw).expanduser()


def save_settings(*, index_folders: list[str] | None = None, memory_workspace: str | None = None,
                  map_export_folder: str | None = None) -> dict:
    """Store the folders in config.json. Paths that don't exist are rejected."""
    from . import brain
    cfg = brain._load_config()
    if index_folders is not None:
        clean = [f.strip() for f in index_folders if isinstance(f, str) and f.strip()][:MAX_FOLDERS]
        missing = [f for f in clean if not Path(f).expanduser().is_dir()]
        if missing:
            raise ValueError("not a folder: " + ", ".join(missing))
        cfg["index_folders"] = clean
    if memory_workspace is not None:
        ws = memory_workspace.strip()
        if ws and not Path(ws).expanduser().is_dir():
            raise ValueError(f"not a folder: {ws}")
        if ws:
            cfg["memory_workspace"] = ws
        else:
            cfg.pop("memory_workspace", None)
    if map_export_folder is not None:
        folder = map_export_folder.strip()
        if folder:
            path = Path(folder).expanduser()
            if not path.is_dir():
                raise ValueError(f"not a folder: {folder}")
            cfg["map_export_folder"] = str(path)
        else:
            cfg.pop("map_export_folder", None)
    brain._save_config(cfg)
    return settings()


def settings() -> dict:
    cfg = _config()
    return {
        "write_vault": str(db.vault_root()),
        "index_folders": list(cfg.get("index_folders") or []),
        "memory_workspace": cfg.get("memory_workspace") or "",
        "map_export_folder": cfg.get("map_export_folder") or "",
        "active_folders": [str(p) for p in index_folders()],
        "active_workspace": str(memory_workspace()),
    }


def map_files() -> set[str]:
    """The map files themselves.

    The map names every project, so indexing it would make the map part of
    the map — and one synthetic note in the middle of the real ones.
    """
    from .notes_structure import MAP_FILENAME

    out: set[str] = set()
    for folder in map_export_destinations():
        try:
            out.add(str((folder / MAP_FILENAME).resolve()))
        except OSError:
            continue
    return out


def map_export_destinations() -> list[Path]:
    """Where the map is also written, so it travels with the notes."""
    cfg = _config()
    raw = (cfg.get("map_export_folder") or "").strip()
    if not raw:
        return []
    path = Path(raw).expanduser()
    return [path] if path.is_dir() else []


def refresh(*, embed: bool = True) -> dict:
    """Incremental re-index of notes, memory and repos, then embeddings for new
    documents when the model is available (the lean desktop build has none).

    After the notes are in, the map is drawn: which project each note
    belongs to, which ideas belong to nobody yet.
    """
    from .indexers import memory, obsidian, repos
    out: dict = {"obsidian": obsidian.run(), "memory": memory.run(), "repos": repos.run()}
    try:
        from . import notes_structure

        out["structure"] = notes_structure.attribute_documents()
        # Keep the map fresh for whoever reads it next. It only rewrites a
        # map that is already there: the product does not start writing
        # into the agent's workspace just because it happened to run.
        refreshed = notes_structure.refresh_agent_map(
            also_to=map_export_destinations())
        if refreshed:
            out["map"] = refreshed
        # What the product would do, written where the user will find it in
        # the morning. It proposes; it does not touch the user's files.
        proposal = notes_structure.write_proposal()
        if proposal:
            out["proposal"] = proposal
    except Exception as e:
        out["structure"] = {"skipped": type(e).__name__}
    if embed:
        try:
            from . import embeddings
            out["embeddings"] = embeddings.run()
        except Exception as e:
            out["embeddings"] = {"skipped": type(e).__name__}
    return out


def status() -> dict:
    with db.db() as conn:
        counts = {r["source"]: r["n"] for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM documents GROUP BY source")}
        last = {r["source"]: r["ended_at"] for r in conn.execute(
            "SELECT source, MAX(ended_at) AS ended_at FROM index_runs "
            "WHERE ended_at IS NOT NULL GROUP BY source")}
    return {**settings(), "documents": counts, "last_indexed": last}
