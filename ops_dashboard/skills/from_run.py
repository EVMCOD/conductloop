"""Draft a skill from a finished agent run (gap 4, step 1).

The draft is a deterministic agentskills.io template built from the run's
recorded prompt, tool calls and output. Secrets, tokens and paths under
HOME are redacted. Nothing is written to the vault until :func:`save` is
called, which validates and copies through the folder importer.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from ..redact import redact_secrets
from . import lint, store
from .catalog import SkillError
from .import_folder import import_from_folder
from .store import skill_dir
from .validate import MAX_DESCRIPTION, MAX_NAME, NAME_RE

# Keep the drafted body comfortably under the importer's 256 KiB SKILL.md cap.
_MAX_SECTION = 8_000
_FINISHED = frozenset({"done", "error"})

_HOME_PATH_RE = lint._HOME_PATH_RE


def _slug(text: str, fallback: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    t = re.sub(r"-{2,}", "-", t)[:MAX_NAME].strip("-")
    if t and NAME_RE.match(t):
        return t
    fb = re.sub(r"[^a-z0-9]+", "-", (fallback or "from-run").lower()).strip("-")[:MAX_NAME]
    return fb if fb and NAME_RE.match(fb) else "from-run"


def redact_draft(text: str | None) -> str:
    """Strip secrets/tokens and replace home-directory prefixes with ``~``."""
    out = redact_secrets(text)
    homes: list[str] = []
    for raw in (os.environ.get("HOME"), os.path.expanduser("~"), str(Path.home())):
        if not raw:
            continue
        try:
            p = Path(raw)
            homes.extend([str(p), str(p.resolve()), p.as_posix()])
        except (OSError, RuntimeError):
            homes.append(str(raw))
    # Longest first so a resolved path is not left half-replaced.
    for prefix in sorted({h for h in homes if h and h not in ("~", "/")}, key=len, reverse=True):
        out = out.replace(prefix, "~")
        out = out.replace(prefix.replace("\\", "/"), "~")
    out = _HOME_PATH_RE.sub("~", out)
    return out


def _run_id(raw: Any) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError) as e:
        raise SkillError("bad_run_id", "run_id must be a positive integer") from e
    if n < 1:
        raise SkillError("bad_run_id", "run_id must be a positive integer")
    return n


def _tool_calls(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
    else:
        return []
    if not isinstance(data, list):
        return []
    return [c for c in data if isinstance(c, dict)]


def _finished_run(raw_id: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    from .. import db_tasks

    rid = _run_id(raw_id)
    run = db_tasks.get_run(rid)
    if not run:
        raise SkillError("run_not_found", f"no run with id {rid}", 404)
    if run.get("status") not in _FINISHED:
        raise SkillError(
            "run_not_finished",
            "only a finished run (done or error) can be saved as a skill",
            409,
        )
    task = db_tasks.get_task(int(run["task_id"]))
    if not task:
        raise SkillError("run_not_found", f"run {rid} has no task", 404)
    return run, task


def _clip(text: str, limit: int = _MAX_SECTION) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _describe(title: str, prompt: str) -> str:
    first = ""
    for line in (prompt or "").splitlines():
        line = line.strip()
        if line:
            first = line
            break
    if first:
        desc = first
    elif title:
        desc = f"Procedure from a finished run of {title}."
    else:
        desc = "Procedure distilled from a finished agent run."
    if len(desc) > MAX_DESCRIPTION:
        desc = desc[: MAX_DESCRIPTION - 1].rstrip() + "…"
    return desc or "Procedure distilled from a finished agent run."


def _format_args(args: Any) -> str:
    if args is None or args == {} or args == []:
        return ""
    if isinstance(args, dict):
        parts = []
        for key in list(args)[:12]:
            val = args[key]
            if isinstance(val, (dict, list)):
                shown = redact_draft(json.dumps(val, ensure_ascii=False, default=str))
            else:
                shown = redact_draft(str(val))
            shown = _clip(shown, 160)
            parts.append(f"{key}={shown}")
        extra = len(args) - 12
        if extra > 0:
            parts.append(f"…(+{extra})")
        return ", ".join(parts)
    return _clip(redact_draft(json.dumps(args, ensure_ascii=False, default=str)), 240)


def _steps(calls: list[dict[str, Any]]) -> str:
    if not calls:
        return "_No tool calls were recorded for this run._"
    lines: list[str] = []
    for i, call in enumerate(calls, 1):
        name = redact_draft(str(call.get("name") or "tool"))
        ok = call.get("ok")
        if ok is True:
            flag = "ok"
        elif ok is False:
            flag = "failed"
        else:
            flag = "recorded"
        args = _format_args(call.get("args"))
        extra = f" — {args}" if args else ""
        lines.append(f"{i}. Used `{name}` ({flag}){extra}.")
    return "\n".join(lines)


def draft_body(run: dict[str, Any], task: dict[str, Any]) -> str:
    title = redact_draft(str(task.get("title") or f"run {run['id']}"))
    prompt = _clip(redact_draft(str(task.get("prompt") or "")))
    steps = _steps(_tool_calls(run.get("tool_calls")))
    provider = redact_draft(str(run.get("provider") or task.get("provider") or "—"))
    model = redact_draft(str(run.get("model") or task.get("model") or "—"))
    output = _clip(redact_draft(str(run.get("output_text") or "")))
    notes = [
        f"- From run #{run['id']} of task #{task['id']} ({run.get('status')}).",
        f"- Provider: {provider} / {model}.",
    ]
    err = run.get("error")
    if err:
        notes.append(f"- Error: {_clip(redact_draft(str(err)), 500)}")
    if output:
        notes.append("- Output:")
        notes.append("")
        notes.append(output)
    else:
        notes.append("- No output was recorded.")
    goal = prompt or title
    return (
        f"# {title}\n\n"
        f"## Goal\n\n{goal}\n\n"
        f"## Steps\n\n{steps}\n\n"
        f"## Inputs\n\n"
        f"- Task title: {title}\n"
        f"- Prompt: {goal}\n\n"
        f"## Notes\n\n" + "\n".join(notes) + "\n"
    )


def preview(run_id: Any) -> dict[str, Any]:
    """Build a draft from a finished run. Does not write to the vault."""
    run, task = _finished_run(run_id)
    title = redact_draft(str(task.get("title") or f"run {run['id']}"))
    prompt = redact_draft(str(task.get("prompt") or ""))
    name = _slug(title, f"from-run-{run['id']}")
    description = _describe(title, prompt)
    body = draft_body(run, task)
    dest = skill_dir(name)
    exists = dest.exists() or dest.is_symlink()
    warnings: list[dict[str, str]] = []
    if exists:
        warnings.append({
            "code": "exists",
            "message": f"a skill named '{name}' already exists in the vault",
        })
    return {
        "ok": True,
        "run_id": run["id"],
        "task_id": task["id"],
        "name": name,
        "description": description,
        "body": body,
        "exists": exists,
        "warnings": warnings,
    }


def save(run_id: Any, name: Any, description: Any, body: Any) -> dict[str, Any]:
    """Validate the edited draft and import it into the vault. Never auto-saves."""
    run, task = _finished_run(run_id)
    if not isinstance(name, str) or not name.strip():
        raise SkillError("name_missing", "frontmatter is missing a 'name'")
    if not isinstance(description, str) or not description.strip():
        raise SkillError(
            "description_missing",
            "'description' is required (what the skill does and when to use it)",
        )
    if not isinstance(body, str) or not body.strip():
        raise SkillError("empty_body", "the Markdown body is empty")
    name = redact_draft(name).strip()
    description = redact_draft(description).strip()
    body = redact_draft(body).strip()
    if len(name) > MAX_NAME or not NAME_RE.match(name):
        raise SkillError(
            "name_format",
            "'name' must be 1-64 lowercase letters, digits and single hyphens, "
            "and must not start or end with a hyphen",
        )
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(
            "description_too_long",
            f"'description' must be at most {MAX_DESCRIPTION} characters ({len(description)} given)",
        )
    skill_md = store.render({"name": name, "description": description}, body)
    tmp_root = Path(tempfile.mkdtemp(prefix="skill-from-run-"))
    folder = tmp_root / name
    try:
        folder.mkdir()
        (folder / "SKILL.md").write_text(skill_md, encoding="utf-8")
        result = import_from_folder(folder)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    result["run_id"] = run["id"]
    result["task_id"] = task["id"]
    from .suggest_from_run import mark_saved
    mark_saved(run["id"])
    return result
