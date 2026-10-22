"""The execution loop: task -> agent (LLM with tools) -> artifact.

This module is the missing piece that turns the ops-dashboard from a read-only
second brain into a real "agent OS". The pattern is simple:

  1. A `Task` is created (project, prompt, optional agent_member_id).
  2. Orchestrator.run_task() resolves the agent (provider/model/system-prompt)
     from the team member (or defaults).
  3. It calls brain.ask() with use_tools=True, so the LLM can call any of the
     read AND write tools registered in tools.py.
  4. Tool calls (read AND write) are recorded per run. The final artifact
     (write_artifact) is linked to the run via artifact_doc_id.
  5. The run finishes with status: done | error | needs_approval.

Providers supported: anthropic, openai, ollama, mock.
Default: ollama (local-first) -> anthropic -> openai -> mock.
"""
from __future__ import annotations

import time

from . import brain, db, projects, provider_errors
from . import tools as brain_tools

# Default system prompt suffix for an executing agent
AGENT_ROLE_PROMPTS = {
    "researcher": (
        "You are a research agent. Your job is to find the most relevant "
        "information in the knowledge base and produce a clear, well-cited "
        "markdown brief. Always cite your sources with [obsidian:file.md] or "
        "[memory:...]. When you have enough information, call write_artifact "
        "to deliver the result. Be concise."
    ),
    "coder": (
        "You are a coding agent. You have access to the knowledge base to "
        "understand the codebase. Produce code or technical artifacts and "
        "call write_artifact to save them. Be precise, use code blocks, and "
        "include brief explanations."
    ),
    "writer": (
        "You are a writer agent. Produce well-structured, opinionated "
        "markdown prose. Use the knowledge base as context. Always end by "
        "calling write_artifact to deliver."
    ),
    "reviewer": (
        "You are a reviewer agent. Read the relevant documents and produce "
        "a concise review with concrete suggestions. End by calling "
        "write_artifact to deliver."
    ),
    "agent": (
        "You are an executing agent. Use the available tools to read the "
        "knowledge base, then produce a markdown artifact with "
        "write_artifact. Be specific, opinionated, and cite your sources."
    ),
}


def _resolve_agent(task: dict) -> tuple[str, str, str]:
    """Pick (provider, model, system_prompt) for a task (display values)."""
    provider, model, system_suffix, _, _ = _resolve_agent_ex(task)
    return provider, model, system_suffix


def _resolve_agent_ex(task: dict) -> tuple[str, str, str, bool, bool]:
    """Like _resolve_agent, plus whether provider/model were *explicitly* chosen
    (by the task row or its team member) rather than auto-picked/defaulted.

    Explicit values are passed through to brain.resolve_provider so the run
    really uses them (strict: no silent provider switch); defaulted ones keep
    the app's historic resolution (env/config model, cooldown fallback).

    Priority:
      1. Explicit task.provider / task.model
      2. team_members row referenced by agent_member_id (role, model, capabilities)
      3. Auto-pick first available provider
    """
    provider = task.get("provider")
    model = task.get("model")
    if model and ":" in model:
        # Accept the "provider:model" form used by team members, also next to
        # an explicit provider. Only a known provider prefix is split off, so
        # Ollama tags such as "llama3.2:3b" stay intact.
        prefix, rest = model.split(":", 1)
        if prefix in brain.DEFAULT_MODELS:
            if provider and provider != prefix:
                raise ValueError(f"model {model!r} names provider {prefix!r}, but provider is {provider!r}")
            provider, model = prefix, rest
    role = "agent"
    system_suffix = AGENT_ROLE_PROMPTS["agent"]

    if task.get("agent_member_id"):
        with db.db() as conn:
            row = conn.execute(
                "SELECT name, role, model, capabilities FROM team_members WHERE id = ?",
                (task["agent_member_id"],),
            ).fetchone()
        if row:
            member = dict(row)
            role = (member.get("role") or "agent").lower()
            if not model and member.get("model"):
                # Parse "anthropic:claude" or "ollama:llama3.2"
                m = member["model"]
                if ":" in m:
                    provider = provider or m.split(":", 1)[0]
                    model = model or m.split(":", 1)[1]
                else:
                    model = m
            # Capabilities may hint at provider
            if not provider and member.get("capabilities"):
                caps = member["capabilities"].lower()
                if "anthropic" in caps or "claude" in caps:
                    provider = "anthropic"
                elif "openai" in caps or "gpt" in caps:
                    provider = "openai"
                elif "ollama" in caps or "local" in caps:
                    provider = "ollama"
            # Use role-specific prompt
            for k in AGENT_ROLE_PROMPTS:
                if k in role:
                    system_suffix = AGENT_ROLE_PROMPTS[k]
                    break

    provider_explicit = bool(provider)
    model_explicit = bool(model)

    # Provider resolution
    if not provider:
        # Default: ollama (local) -> anthropic -> openai -> mock
        for p in ("ollama", "anthropic", "openai"):
            st = brain.list_providers_status()
            if any(s["name"] == p and s["available"] for s in st):
                provider = p
                break
        if not provider:
            provider = "mock"

    # Use a provider-specific default model if not set
    if not model:
        model = brain.DEFAULT_MODELS.get(provider, "mock-1")

    return provider, model, system_suffix, provider_explicit, model_explicit


def _build_system_prompt(project_key: str | None, system_suffix: str) -> str:
    today = time.strftime("%Y-%m-%d")
    project_ctx = ""
    if project_key:
        proj = projects.find_project(project_key)
        if proj:
            n_urls = len(proj.url_names or [])
            n_repos = len(proj.repo_paths or [])
            project_ctx = (
                f"\n\nYou are working on the **{proj.name}** project ({proj.key}). "
                f"{n_repos} repos · {n_urls} tracked URLs."
            )
    return (
        f"Today's date: {today}.\n\n"
        f"{system_suffix}{project_ctx}\n\n"
        f"=== CRITICAL INSTRUCTION ===\n"
        f"After gathering context with the read tools, you MUST call "
        f"write_artifact to deliver your result. Do not respond with plain "
        f"text alone — always end with a write_artifact call so the result "
        f"is persisted to the vault and indexed. The write_artifact tool "
        f"takes (project, title, markdown). Pass the project key as-is "
        f"(e.g. 'traincurve'). The title should be a concise label "
        f"(e.g. 'Weekly brief 2026-06-20'). The markdown should be the full "
        f"deliverable, well-formatted, and cited.\n"
        f"===========================\n"
    )


def run_task(task_id: int, *, sync: bool = True, on_event=None) -> dict:
    """Execute a task. Returns a dict with run_id and final status.

    If sync is False, returns immediately and the run continues in the
    background (used by the API when the user submits a task and we don't
    want to block the HTTP request).

    on_event(event: dict) is called for each progress event when provided.
    Useful for streaming SSE to the client. Events:
      {type: "start", run_id, provider, model}
      {type: "text", text}              — final text of an iteration
      {type: "tool_call", name, args, ok, result}  — a tool was called
      {type: "artifact", doc_id, path}   — write_artifact succeeded
      {type: "done", status, duration_ms, artifact_doc_id, output_path}
    """
    task = db.get_task(task_id)
    if not task:
        return {"ok": False, "error": f"task {task_id} not found"}

    if task["status"] == "running":
        return {"ok": False, "error": "task is already running"}

    try:
        provider, model, system_suffix, provider_explicit, model_explicit = _resolve_agent_ex(task)
    except ValueError as e:                      # e.g. provider "openai" with "anthropic:…"
        return {"ok": False, "error": str(e)}
    system_prompt = _build_system_prompt(task.get("project_key"), system_suffix)
    run_id = db.start_run(task_id, provider, model)
    db.add_activity("task_started", f"{task['title'][:80]} → {provider}/{model}", task.get("project_key"))

    def _emit(event: dict) -> None:
        if on_event:
            try:
                on_event(event)
            except Exception:
                pass

    unavailable: dict = {}  # provider_unavailable payload, if the run hit it

    def _execute():
        _emit({"type": "start", "run_id": run_id, "provider": provider, "model": model})
        t0 = time.perf_counter()
        tool_log: list[dict] = []
        output_text = ""
        output_path = None
        artifact_doc_id = None
        tokens_in = 0
        tokens_out = 0
        status = "done"
        error_msg = None

        try:
            # Explicit task/member provider+model are honoured exactly (strict:
            # a cooling-down provider errors instead of switching). Defaults
            # keep the historic resolution so single-agent behaviour is unchanged.
            prov = brain.resolve_provider(
                provider,
                model=model if model_explicit else None,
                strict=provider_explicit,
            )
            # We want write tools available. Use the anthropic/openai tool list
            # depending on provider.
            if prov.name == "anthropic":
                prov_tools = brain_tools.tools_for_anthropic()
            else:
                prov_tools = brain_tools.tools_for_openai()

            messages = [{"role": "user", "content": task["prompt"]}]

            def _on_call(name, args, result):
                nonlocal artifact_doc_id, output_path
                tool_log.append({
                    "name": name,
                    "args": args,
                    "ok": result.get("ok", False),
                    "ts": int(time.time()),
                })
                # Stream the tool call to listeners
                inner = result.get("result") if isinstance(result.get("result"), dict) else {}
                _emit({
                    "type": "tool_call",
                    "name": name,
                    "args": args,
                    "ok": result.get("ok", False),
                    "result": inner,
                })
                # If this was a write_artifact, capture the doc_id.
                # tools.py wraps results in {"ok": True, "result": {...}} — the doc_id
                # is inside the inner "result" dict.
                if name == "write_artifact" and result.get("ok"):
                    res = result.get("result") or {}
                    if isinstance(res, dict):
                        if res.get("doc_id"):
                            artifact_doc_id = res["doc_id"]
                        if res.get("path"):
                            output_path = res["path"]
                        _emit({"type": "artifact", "doc_id": artifact_doc_id, "path": output_path,
                               "staged": bool(res.get("staged"))})

            if prov.supports_tools():
                output_text = prov.ask_with_tools(
                    system_prompt, messages, prov_tools, on_tool_call=_on_call,
                    max_tokens=2000, temperature=0.3,
                )
            else:
                output_text = prov.ask(
                    system_prompt, messages, max_tokens=2000, temperature=0.3,
                )

            # Stream the final text of the agent
            if output_text:
                _emit({"type": "text", "text": output_text})

            # If the tool path returned a final text but write_artifact was staged
            # (OPS_REQUIRE_APPROVAL on), the run is "needs_approval"
            if any(t["name"] == "write_artifact" and t["ok"] and (t["args"].get("staged") or
                                                                   (isinstance(t["args"], dict) and t["args"].get("staged"))) for t in tool_log):
                status = "needs_approval"
            elif any(t["name"] == "write_artifact" and t["ok"] for t in tool_log):
                status = "done"
            else:
                status = "done"  # Agent produced text but no artifact

        except brain.ProviderUnavailableError as e:
            status = "error"
            error_msg = provider_errors.run_error_text(e)
            unavailable.update(e.payload())
        except Exception as e:
            status = "error"
            error_msg = f"{type(e).__name__}: {e}"

        duration_ms = int((time.perf_counter() - t0) * 1000)
        db.finish_run(
            run_id,
            status=status,
            output_path=output_path,
            artifact_doc_id=artifact_doc_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=duration_ms,
            tool_calls=tool_log,
            output_text=output_text,
            error=error_msg,
        )
        db.add_activity(
            f"task_{status}",
            f"{task['title'][:80]} ({provider}/{model}, {duration_ms}ms)",
            task.get("project_key"),
        )
        _emit({
            "type": "done",
            "status": status,
            "duration_ms": duration_ms,
            "artifact_doc_id": artifact_doc_id,
            "output_path": output_path,
            "error": error_msg,
            "provider_unavailable": dict(unavailable) or None,
        })

    if sync:
        _execute()
    else:
        import threading
        t = threading.Thread(target=_execute, daemon=True, name=f"task-{task_id}")
        t.start()

    if unavailable:  # sync run refused by the provider: stable 503 payload
        return {**unavailable, "task_id": task_id, "run_id": run_id}
    return {
        "ok": True,
        "task_id": task_id,
        "run_id": run_id,
        "provider": provider,
        "model": model,
    }


def re_run(task_id: int, *, sync: bool = True) -> dict:
    """Re-run a task: always creates a NEW run row."""
    task = db.get_task(task_id)
    if not task:
        return {"ok": False, "error": f"task {task_id} not found"}
    # Reset status so start_run() works
    db.update_task(task_id, status="queued")
    return run_task(task_id, sync=sync)


def approve_run(run_id: int = None, task_id: int = None) -> dict:
    """If a run is in 'needs_approval', the user has approved it via UI.
    We re-execute the write_artifact call (since the original was staged).
    For now, this is a no-op stub; the actual artifact landing is the UI's job.

    Accepts either run_id (the existing API) or task_id (resolves to the
    latest run for that task, which is what the UI uses).
    """
    run = None
    if run_id is not None:
        run = db.get_run(run_id)
    elif task_id is not None:
        with db.db() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if row:
            run = dict(row)
    if not run:
        return {"ok": False, "error": "no run found for the given id"}
    if run["status"] != "needs_approval":
        return {"ok": False, "error": f"run status is '{run['status']}', not needs_approval"}
    # For the MVP, "approving" means: turn off OPS_REQUIRE_APPROVAL and re-run
    # the write. The user can do this in a future iteration. For now, just
    # mark the run as approved (alias for 'done' since the artifact was never
    # actually written).
    db.finish_run(run_id, status="done", output_text=run.get("output_text") or "(approved by user; artifact was not written under OPS_REQUIRE_APPROVAL=on)", error=None)
    return {"ok": True, "run_id": run_id, "status": "done"}
