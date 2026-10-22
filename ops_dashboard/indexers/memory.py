"""Memory + daily notes indexer.

Reads:
  - $OPS_WORKSPACE/MEMORY.md
  - $OPS_WORKSPACE/memory/*.md
  - $OPS_WORKSPACE/HEARTBEAT.md
  - $OPS_WORKSPACE/IDENTITY.md
  - $OPS_WORKSPACE/USER.md
  - $OPS_WORKSPACE/AGENTS.md
  - $OPS_WORKSPACE/SOUL.md
  - $OPS_WORKSPACE/TOOLS.md
  - $OPS_VAULT_ROOT/daily/*.md  (also indexed by obsidian; we add as 'memory' for explicit daily notes)
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import db

WORKSPACE = Path(os.environ.get("OPS_WORKSPACE", str(Path.home() / ".ops-workspace"))).expanduser()
DAILY_DIR = db.vault_root() / "daily"

WORKSPACE_FILES = [
    "MEMORY.md",
    "HEARTBEAT.md",
    "IDENTITY.md",
    "USER.md",
    "AGENTS.md",
    "SOUL.md",
    "TOOLS.md",
    "CLAUDE.md",
]


def _index_file(path: Path, *, source: str = "memory", metadata_extra: dict | None = None) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  ! read error {path}: {e}")
        return {"indexed": 0, "skipped": 0, "errors": 1}

    stat = path.stat()
    title = path.stem
    metadata = {"rel": str(path.relative_to(Path.home()))}
    if metadata_extra:
        metadata.update(metadata_extra)

    was_upserted = db.upsert_document(
        source=source,
        path=str(path),
        title=title,
        content=text,
        metadata=metadata,
        mtime=int(stat.st_mtime),
        size=stat.st_size,
    )
    if was_upserted:
        return {"indexed": 1, "skipped": 0, "errors": 0}
    return {"indexed": 0, "skipped": 1, "errors": 0}


def run() -> dict:
    run_id = db.start_index_run("memory")
    total = {"indexed": 0, "skipped": 0, "errors": 0}

    # Workspace root files
    for name in WORKSPACE_FILES:
        path = WORKSPACE / name
        if path.exists():
            c = _index_file(path, source="memory")
        else:
            c = {"indexed": 0, "skipped": 0, "errors": 0}
        for k, v in c.items():
            total[k] += v

    # Memory directory
    mem_dir = WORKSPACE / "memory"
    if mem_dir.exists():
        for path in mem_dir.glob("*.md"):
            c = _index_file(path, source="memory", metadata_extra={"kind": "daily_or_note"})
            for k, v in c.items():
                total[k] += v

    db.end_run(run_id, **total, notes=f"workspace={WORKSPACE}")
    print(f"memory: done. indexed={total['indexed']} skipped={total['skipped']} errors={total['errors']}")
    return total
