"""Todoist (personal API token, Todoist API v1) — read + ingest active tasks
into the vault; writes (create a task, close a task) always wait for
confirmation.

There was no Todoist connection before, so the token lives only in the OS
keychain (``connector_tokens``). Todoist personal tokens are full-access (no
read-only scope): the local permission stays ``read`` until the user grants
writes, and every write is still confirmed one by one.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from urllib.parse import quote

from .. import connector_registry as registry
from . import base

API = "https://api.todoist.com/api/v1"
ID_RE = r"[A-Za-z0-9_\-]{1,64}"
PAGE = 200
MAX_PAGES = 5
PRIORITY = {4: "p1", 3: "p2", 2: "p3", 1: "p4"}    # API 4 = most urgent (shown as p1 in the app)


def _ts(iso: Any) -> int:
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _paged(c: base.TokenClient, path: str, params: dict | None = None) -> Iterator[dict]:
    cursor = None
    for _ in range(MAX_PAGES):
        q = {"limit": PAGE, **(params or {})}
        if cursor:
            q["cursor"] = cursor
        res = c.get_json(f"{API}/{path}", params=q)
        yield from res.get("results") or []
        cursor = res.get("next_cursor")
        if not cursor:
            return
    yield {"__partial__": True}          # stopped with pages left


def whoami(c: base.TokenClient) -> str:
    me = c.get_json(f"{API}/user")
    return str(me.get("full_name") or me.get("email") or "")


def _projects(c: base.TokenClient) -> dict[str, str]:
    return {str(p.get("id")): str(p.get("name") or "") for p in _paged(c, "projects")
            if not p.get("__partial__")}


def sync(c: base.TokenClient, config: dict) -> Iterator[base.Item]:
    projects = _projects(c)
    wanted = {n.casefold() for n in config.get("projects") or []}
    ids = [pid for pid, name in projects.items() if name.casefold() in wanted] if wanted else [None]
    if wanted and not ids:
        raise base.TokenError("unknown_project", "none of the configured projects exists in Todoist", 400)
    for pid in ids:
        for t in _paged(c, "tasks", {"project_id": pid} if pid else None):
            if t.get("__partial__"):
                yield base.PARTIAL
                continue
            due = t.get("due") or {}
            proj = projects.get(str(t.get("project_id")), "")
            head = " · ".join(x for x in (proj, PRIORITY.get(int(t.get("priority") or 1), ""),
                                          f"vence {due.get('string') or due.get('date')}" if due else "",
                                          ", ".join(t.get("labels") or [])) if x)
            yield base.Item(kind="task", key=str(t.get("id")), title=str(t.get("content") or ""),
                            content=f"{head}\n\n{t.get('description') or ''}",
                            url=f"https://app.todoist.com/app/task/{quote(str(t.get('id')))}",
                            updated=_ts(t.get("updated_at") or t.get("added_at")),
                            tags=tuple(["todoist", proj, *(t.get("labels") or [])][:10]))


def _create_payload(p: dict) -> dict[str, Any]:
    content = p.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > 500:
        raise base.TokenError("bad_content", "content is required (<= 500 chars)")
    body: dict[str, Any] = {"content": content.strip()}
    for k, lim in (("description", 5000), ("due_string", 100)):
        v = p.get(k)
        if v is not None:
            if not isinstance(v, str) or len(v) > lim:
                raise base.TokenError(f"bad_{k}", f"{k} must be a string (<= {lim})")
            body[k] = v
    if p.get("project") is not None and (not isinstance(p["project"], str) or len(p["project"]) > 120):
        raise base.TokenError("bad_project", "project must be a project name")
    return body


def create_task(c: base.TokenClient, payload: dict) -> dict:
    body = _create_payload(payload)
    if payload.get("project"):
        match = [pid for pid, n in _projects(c).items() if n.casefold() == str(payload["project"]).casefold()]
        if not match:
            raise base.TokenError("unknown_project", str(payload["project"])[:120], 400)
        body["project_id"] = match[0]
    res = c.post_json(f"{API}/tasks", body)
    return {"id": res.get("id"), "content": res.get("content")}


def _task_id(p: dict) -> str:
    tid = p.get("task_id")
    if not isinstance(tid, str) or not re.fullmatch(ID_RE, tid):
        raise base.TokenError("bad_task_id", "task_id must be a Todoist task id")
    return tid


def close_task(c: base.TokenClient, payload: dict) -> dict:
    tid = _task_id(payload)
    c.post_json(f"{API}/tasks/{quote(tid)}/close", None)
    return {"closed": tid}


def _sum_create(p: dict) -> str:
    _create_payload(p)
    extra = f" en {p['project']}" if p.get("project") else ""
    due = f" (vence {p['due_string']})" if p.get("due_string") else ""
    return f"Todoist: crear tarea «{str(p.get('content'))[:200]}»{extra}{due}"


def _sum_close(p: dict) -> str:
    tid = _task_id(p)
    # Only task_id is sent to Todoist; "content" is the caller's unverified claim.
    claim = f" (el agente dice que es «{str(p['content'])[:200]}»)" if p.get("content") else ""
    return f"Todoist: completar la tarea {tid}{claim}"


CONNECTOR = base.register_connector(base.TokenConnector(
    spec=registry.ConnectorSpec(
        id="todoist", name="Todoist", auth="token", allowed_hosts=("api.todoist.com",),
        docs_url="https://app.todoist.com/app/settings/integrations/developer",
        actions={
            "sync": registry.ActionSpec("sync", "read", "Import active tasks into the vault", base.sync_action("todoist")),
            "create_task": registry.ActionSpec("create_task", "write", "Create a task",
                                               base.token_action("todoist", create_task), summarize=_sum_create),
            "close_task": registry.ActionSpec("close_task", "write", "Complete a task",
                                              base.token_action("todoist", close_task), summarize=_sum_close),
        }),
    token_mode="keychain", whoami=whoami, sync=sync, snapshot_kinds=("task",),
    config_fields=(base.ConfigField("projects", "list", "Proyectos (nombre, uno por línea; vacío = todas las tareas activas)",
                                    "Projects (name, one per line; empty = all active tasks)",
                                    pattern=r"[^\x00-\x1f]{1,120}", max_items=30),),
    token_help_es="Pega tu token de API (Ajustes → Integraciones → Desarrollador). Se guarda solo en el llavero. El token de Todoist da acceso completo: las escrituras siguen desactivadas aquí hasta que las permitas, y cada una espera tu confirmación.",
    token_help_en="Paste your API token (Settings → Integrations → Developer). It is stored only in the keychain. Todoist tokens are full-access: writes stay off here until you allow them, and each one waits for your confirmation.",
))
