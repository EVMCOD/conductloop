"""Tools the brain can invoke (function calling).

Each tool is a function that takes a dict of args and returns a dict.
The brain can call any of these to get live data, not just text from the
retrieved context.

Wire format for LLM providers:
  - Anthropic: tools=[{"name", "description", "input_schema"}]
  - OpenAI:    tools=[{"type": "function", "function": {"name", "description", "parameters"}}]
  - Ollama:    same as OpenAI (newer versions)
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path as _Path
from typing import Any

from . import db, projects
from .indexers import live_urls

log = logging.getLogger(__name__)


def _ok(payload: Any) -> dict:
    return {"ok": True, "result": payload}


def _err(message: str) -> dict:
    return {"ok": False, "error": message}


# ---- Tool definitions ----

TOOLS: list[dict] = [
    {
        "name": "list_failing_urls",
        "description": "Returns URLs that are currently failing (non-2xx) with their status code and latency.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_url_health",
        "description": "Returns the health of all known live URLs (name, URL, status, latency, last check).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Optional project key to filter by (e.g. 'my-project')."},
            },
            "required": [],
        },
    },
    {
        "name": "list_open_todos",
        "description": "Returns open (not-done) TODO items, optionally filtered by project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Optional project key (e.g. 'my-project')."},
                "limit": {"type": "integer", "description": "Max number of items. Default 20."},
            },
            "required": [],
        },
    },
    {
        "name": "project_status",
        "description": "Returns a status summary for one project: phase, playbook progress, repo count, URL health, mention count, last activity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project key (e.g. 'my-project')."},
            },
            "required": ["project"],
        },
    },
    {
        "name": "vault_search",
        "description": "Searches the Obsidian vault + memory for a query string and returns up to N matching documents with title, path, and excerpt.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (text)."},
                "limit": {"type": "integer", "description": "Max results. Default 10."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "diff_since_yesterday",
        "description": "Returns the diff between today's snapshot and the previous one: doc count delta, URL changes, TODO open delta, project activity changes.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "recent_documents",
        "description": "Returns the most recently modified documents, optionally filtered by source (obsidian, memory, repo, playbook).",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source filter: obsidian, memory, repo, or playbook."},
                "limit": {"type": "integer", "description": "Max results. Default 10."},
            },
            "required": [],
        },
    },
    # ---- WRITE tools (the loop) ----
    {
        "name": "write_artifact",
        "description": (
            "Writes a markdown artifact to the agent-managed folder of the vault "
            "(_agents/<project>/) and indexes it into the knowledge base. "
            "Use this to deliver the result of your task. Returns the new doc_id. "
            "The artifact will appear in search and the graph immediately. "
            "If OPS_REQUIRE_APPROVAL is on, the artifact is staged for review instead "
            "of being written, and the task status becomes 'needs_approval'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project key (e.g. 'my-project'). Required."},
                "title":   {"type": "string", "description": "Artifact title. Used as filename and document title."},
                "markdown": {"type": "string", "description": "Full markdown content of the artifact."},
                "tags":    {"type": "array", "items": {"type": "string"}, "description": "Optional tags to apply."},
            },
            "required": ["project", "title", "markdown"],
        },
    },
    {
        "name": "create_todo",
        "description": "Creates a new user-visible TODO (different from agent tasks). Use for action items you surface to the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Optional project key."},
                "text":    {"type": "string", "description": "TODO text. Concise, action-oriented."},
                "priority": {"type": "integer", "description": "0 = normal, 1 = high."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "append_to_daily",
        "description": "Appends a section to today's daily note (<vault>/daily/YYYY-MM-DD.md). Useful for 'log this insight' style tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "heading": {"type": "string", "description": "Section heading (e.g. '## Agent note')."},
                "text":    {"type": "string", "description": "Body text to append."},
            },
            "required": ["heading", "text"],
        },
    },
]


# ---- Tool implementations ----

def _impl_list_failing_urls(args: dict) -> dict:
    snap = live_urls.latest()
    failing = [u for u in snap if not u["ok"]]
    return _ok(failing)


def _impl_list_url_health(args: dict) -> dict:
    project = args.get("project")
    snap = live_urls.latest()
    if project:
        proj = projects.find_project(project)
        if not proj:
            return _err(f"unknown project: {project}")
        names = {u["name"] for u in proj.urls}
        snap = [u for u in snap if u["name"] in names]
    return _ok(snap)


def _impl_list_open_todos(args: dict) -> dict:
    project = args.get("project")
    limit = int(args.get("limit") or 20)
    rows = db.list_todos(project=project, only_open=True, limit=limit)
    return _ok([dict(r) for r in rows])


def _impl_project_status(args: dict) -> dict:
    project = args.get("project")
    if not project:
        return _err("project is required")
    proj = projects.find_project(project)
    if not proj:
        return _err(f"unknown project: {project}")
    pb = db.get_playbook(proj.key)
    rows = projects.all_projects_summary()
    summary = next((r for r in rows if r["key"] == proj.key), None)
    return _ok({
        "project": proj.name,
        "key": proj.key,
        "phase": pb.get("phase"),
        "playbook_total": sum(1 for e in pb.get("entries", []) if e["entry_kind"] != "phase"),
        "playbook_done": sum(1 for e in pb.get("entries", []) if e["entry_kind"] != "phase" and e["done"]),
        "repos": [r["name"] for r in (summary or {}).get("repos", [])],
        "urls_ok": (summary or {}).get("url_ok", 0),
        "urls_total": (summary or {}).get("url_total", 0),
        "mentions": (summary or {}).get("mention_count", 0),
        "last_activity": (summary or {}).get("last_activity"),
    })


def _impl_vault_search(args: dict) -> dict:
    from .redact import redact_secrets
    q = args.get("query", "")
    if not q:
        return _err("query is required")
    limit = int(args.get("limit") or 10)
    rows = db.search(q, limit=limit)
    # Browser PR 7: web clips are untrusted third-party text; never feed them
    # into the tool loop (it has write tools).
    rows = [r for r in rows if r["source"] != "web"]
    out = []
    for r in rows:
        out.append({
            "source": r["source"],
            # Tool results are fed back to the LLM: never hand it a credential,
            # wherever it sits (filename, H1 or body).
            "title": redact_secrets(str(r["title"] or "")),
            "path": redact_secrets(str(r["path"] or "")),
            "excerpt": redact_secrets((r["excerpt"] or "")[:300]),
        })
    return _ok(out)


def _impl_diff_since_yesterday(args: dict) -> dict:
    from . import snapshots
    return _ok(snapshots.diff())


def _impl_recent_documents(args: dict) -> dict:
    source = args.get("source")
    limit = int(args.get("limit") or 10)
    rows = db.recent(source=source, limit=limit)
    return _ok([dict(r) for r in rows if r["source"] != "web"])  # browser PR 7: see vault_search


# ---- WRITE tool implementations (the loop) ----

import re
from datetime import date as _date


def _agent_vault_root() -> _Path:
    """Where agent artifacts go. Resolved per call so a vault chosen in the app
    applies without a restart."""
    override = os.environ.get("OPS_AGENT_VAULT")
    return _Path(override) if override else db.vault_root() / "_agents"


def _slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:80] or "artifact"


def _now() -> int:
    return int(time.time())


def _impl_write_artifact(args: dict) -> dict:
    """Write a markdown artifact to vault/_agents/<project>/<slug>-<ts>.md and index it."""
    from . import embeddings  # local import to avoid cycle
    project = (args.get("project") or "").strip().lower()
    title   = (args.get("title")   or "").strip()
    md      = args.get("markdown") or ""
    tags    = args.get("tags") or []

    if not project:  return _err("project is required")
    if not title:    return _err("title is required")
    if not md.strip(): return _err("markdown is empty")
    if len(md) > 200_000:
        return _err(f"markdown too long ({len(md)} chars; max 200000)")

    require_approval = (os.environ.get("OPS_REQUIRE_APPROVAL") or "").lower() in ("1", "true", "yes", "on")
    if require_approval:
        return _ok({
            "staged": True,
            "project": project,
            "title": title,
            "length": len(md),
            "message": "OPS_REQUIRE_APPROVAL is on. Artifact NOT written; task is now needs_approval and the user must approve in the UI before it lands in the vault.",
        })

    # Build the file path
    folder = _agent_vault_root() / project
    folder.mkdir(parents=True, exist_ok=True)
    slug = _slugify(title)
    ts = _date.today().isoformat()
    # Add a short timestamp suffix to avoid collisions
    filename = f"{slug}-{ts}.md"
    path = folder / filename
    if path.exists():
        # Add a numeric suffix until unique
        i = 2
        while True:
            cand = folder / f"{slug}-{ts}-{i}.md"
            if not cand.exists():
                path = cand
                break
            i += 1

    # Prepend a small frontmatter so the obsidian indexer can pick up project + tags
    frontmatter = (
        f"---\n"
        f"project: {project}\n"
        f"title: {title}\n"
        f"created: {ts}\n"
        f"source: agents\n"
        + (f"tags: [{', '.join(tags)}]\n" if tags else "")
        + "---\n\n"
    )
    final_md = frontmatter + md
    path.write_text(final_md, encoding="utf-8")

    # Index into the documents table
    _vault = db.vault_root()
    rel_path = str(path.relative_to(_vault)) if str(path).startswith(str(_vault)) else str(path)
    try:
        with db.db() as conn:
            # Remove any old doc at the same path (re-write)
            old = conn.execute("SELECT id FROM documents WHERE path = ?", (rel_path,)).fetchone()
            if old:
                conn.execute("DELETE FROM documents WHERE id = ?", (old["id"],))
            cur = conn.execute(
                "INSERT INTO documents (source, path, title, content, mtime, indexed_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("obsidian", rel_path, title, final_md, _now(), _now()),
            )
            doc_id = int(cur.lastrowid or 0)
            # Apply tags
            for t in tags:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO doc_tags (doc_id, tag, added_at) VALUES (?, ?, ?)",
                        (doc_id, str(t).lower(), _now()),
                    )
                except Exception: pass
            # Try to embed
            try:
                embeddings.index_document(doc_id, final_md)
            except Exception:
                pass  # embedding is best-effort
    except Exception as e:
        return _err(f"wrote file but failed to index: {e}")

    # Activity feed entry
    try:
        db.add_activity("agent_artifact", f"{project}: {title}", project)
    except Exception: pass

    return _ok({
        "doc_id": doc_id,
        "path": str(path),
        "rel_path": rel_path,
        "size": len(final_md),
        "message": f"Artifact '{title}' written to vault and indexed. It is now searchable and appears in the graph linked to '{project}'.",
    })


def _impl_create_todo(args: dict) -> dict:
    project = args.get("project")
    text = (args.get("text") or "").strip()
    priority = int(args.get("priority") or 0)
    if not text:
        return _err("text is required")
    if len(text) > 500:
        text = text[:500]
    tid = db.add_user_todo(text, project_key=project, priority=priority)
    return _ok({"id": tid, "text": text, "project": project})


def _impl_append_to_daily(args: dict) -> dict:
    heading = (args.get("heading") or "## Note").strip()
    text    = (args.get("text") or "").strip()
    if not text:
        return _err("text is required")
    daily_root = _Path(os.environ.get(
        "OPS_DAILY_VAULT",
        str(db.vault_root() / "daily")
    ))
    daily_root.mkdir(parents=True, exist_ok=True)
    today = _date.today().isoformat()
    path = daily_root / f"{today}.md"
    ts_line = f"\n{heading}\n_added {time.strftime('%H:%M')}_\n\n{text}\n"
    if not path.exists():
        path.write_text(f"# {today}\n" + ts_line, encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as f:
            f.write(ts_line)
    return _ok({"path": str(path), "appended_chars": len(ts_line)})


IMPL: dict[str, Callable[[dict], dict]] = {
    "list_failing_urls":  _impl_list_failing_urls,
    "list_url_health":    _impl_list_url_health,
    "list_open_todos":    _impl_list_open_todos,
    "project_status":     _impl_project_status,
    "vault_search":       _impl_vault_search,
    "diff_since_yesterday": _impl_diff_since_yesterday,
    "recent_documents":   _impl_recent_documents,
    "write_artifact":     _impl_write_artifact,
    "create_todo":        _impl_create_todo,
    "append_to_daily":    _impl_append_to_daily,
}

# Fix #4: explicit allow-list of accepted args per tool. If the LLM passes
# anything outside this set, log a warning (not a hard error — some tools have
# optional kwargs they ignore on purpose). This catches the silent-misuse case
# (e.g. list_open_todos filters by status, model says status=open, but accidentally
# passes status_open as a typo'd key). Bug fixed: 'silent ignore of bad_args'.
_KNOWN_ARGS: dict[str, set[str]] = {
    "list_failing_urls":   {"limit"},
    "list_url_health":     set(),
    "list_open_todos":     {"project_key", "status", "limit", "since_days"},
    "project_status":      {"project_key"},
    "vault_search":        {"query", "limit", "since_days"},
    "diff_since_yesterday": {"project_key"},
    "recent_documents":    {"limit", "since_days"},
    "write_artifact":      {"project", "title", "body", "tags"},
    "create_todo":         {"text", "project", "priority"},
    "append_to_daily":     {"text", "heading", "confirm"},
}

# Fix #6: tools that mutate user data need an explicit confirm flag.
_WRITE_TOOLS: set[str] = {"create_todo", "write_artifact", "append_to_daily"}


def _activate_skill(args: dict) -> dict:
    """L5: progressive disclosure of agentskills.io skills (read only)."""
    from .skills import catalog
    return catalog.activate((args or {}).get("name"))


def _all_tools() -> list[dict]:
    """Static TOOLS + activate_skill, registered only when the catalog has
    enabled skills (its ``name`` is an enum of those skills)."""
    from .skills import catalog
    spec = catalog.tool_spec()
    return TOOLS + [spec] if spec else TOOLS


def call(name: str, args: dict) -> dict:
    if name == "activate_skill":
        return _activate_skill(args)
    fn = IMPL.get(name)
    if not fn:
        return _err(f"unknown tool: {name}")
    # Fix #4: warn about unknown args the LLM may have passed by mistake
    allowed = _KNOWN_ARGS.get(name)
    if allowed is not None and isinstance(args, dict):
        unknown = set(args.keys()) - allowed
        if unknown:
            log.warning(
                "tool %s got unknown args %s (known: %s). Model likely passed "
                "a mis-spelled or unsupported argument.",
                name, sorted(unknown), sorted(allowed),
            )
            # Surface the warning to the caller so it can be observed in audit:
            # we don't hard-fail because some impls accept extra kwargs by design.
    # Fix #6: writes require confirm=true (or dry_run=true)
    if name in _WRITE_TOOLS:
        # append_to_daily is special — it considers any non-empty confirm as yes
        if name == "append_to_daily":
            if not (args.get("confirm") or args.get("dry_run")):
                # Dry-run preview so the model gets a useful response (not blocked
                # completely), but nothing is written.
                preview = {
                    "dry_run": True,
                    "would_write_to": str(_Path(os.environ.get(
                        "OPS_DAILY_VAULT",
                        str(db.vault_root() / "daily")
                    )) / f"{_date.today().isoformat()}.md"),
                    "heading": args.get("heading", "## Note"),
                    "text_length": len((args.get("text") or "").strip()),
                    "instruction": (
                        "Set `confirm: true` to actually write. The user can "
                        "also explicitly authorize this tool call inline."
                    ),
                }
                return _ok(preview)
        # create_todo / write_artifact require explicit confirm too
        elif name in ("create_todo", "write_artifact"):
            if not args.get("confirm"):
                return _err(
                    f"{name} is a write tool. Pass `confirm: true` (or `dry_run: true`) "
                    "to acknowledge the user asked for this write. The user-facing "
                    "ask UI should prompt before invoking."
                )
    try:
        return fn(args)
    except Exception as e:
        log.exception("tool %s crashed", name)
        return _err(f"tool {name} crashed: {e}")


# ---- Provider adapters for tool calling ----

def tools_for_anthropic() -> list[dict]:
    return _all_tools()


def tools_for_openai() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in _all_tools()
    ]


def tools_for_ollama() -> list[dict]:
    return tools_for_openai()
