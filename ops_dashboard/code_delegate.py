"""From the chat to a code session: paste a coding task in the chat and it runs.

A code session (``ops_dashboard/sessions``) does its work in a copy of the
repository, on its own ``cl/…`` branch, with no network and no way to push.
Starting one used to take the /sessions page: a repository, a form, a task, a
start button. Other agent apps take a pasted prompt in the chat and do it, and
that is what a person expects.

So the chat recognises a request to change code in a repository and, instead of
having the team answer it in words (they cannot see the repository), opens a
code session on a marked repository with the chat's own provider, model and key,
gives it the message as its task and starts it. The chat shows the session as a
card that follows it live; at the end the person reads the report, looks at the
changes and, with one click, pushes the branch and opens the pull request
(``POST /api/sessions/<sid>/publish``). The session itself still never pushes.

Recognising the request takes two steps, so that a normal question is never
turned into a session: a quick textual filter (``looks_like_code``), and only
when it matches, one short question to the model (``is_code_task``). When the
model cannot answer, the message stays a chat message.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger("conductloop.code_delegate")

CLASSIFY_DEADLINE_SECONDS = 15.0

# A long session is the point of delegating: the chat does not wait for it.
MAX_ITERATIONS = 60

_FILE = re.compile(r"[\w./-]+\.(?:py|js|mjs|ts|tsx|jsx|rs|go|java|kt|swift|rb|php|cs|c|h|cpp|"
                   r"css|scss|html|md|json|toml|ya?ml|sh|sql|ini|cfg)\b", re.I)
_WORDS = re.compile(r"\b(?:commit|commits|pytest|ruff|lint|unit tests?|tests?|refactor\w*|bug|"
                    r"repo(?:sitory|sitorio)?|pull request|branch|rama|function|función|funcion|"
                    r"fichero|archivo|código|codigo|code|implement\w*|arregl\w*|fix\w*|endpoint|"
                    r"migration|migración|docstring|changelog|readme)\b", re.I)

CLASSIFY_SYSTEM = (
    "You sort requests sent to a team of AI agents. Answer with exactly one word.\n"
    "CODE: the person asks to change files in a software repository: write or edit "
    "code, fix a bug, add or change tests, update the repository's docs, commit.\n"
    "CHAT: anything else, including questions about code that do not ask to change "
    "it, plans, opinions, and requests about notes, emails or the web."
)


def looks_like_code(message: str) -> bool:
    """A cheap first filter: a file name, or two words of the trade."""
    text = message or ""
    if _FILE.search(text):
        return True
    return len({m.group(0).lower() for m in _WORDS.finditer(text)}) >= 2


def is_code_task(provider: Any, message: str) -> bool:
    """Whether ``message`` asks to change code in a repository.

    Only asks the model when the textual filter matches; a model that does not
    answer in time (or fails) leaves the message in the chat.
    """
    if not looks_like_code(message):
        return False
    if getattr(provider, "name", "") == "mock":
        return False
    from . import brain

    try:
        raw = brain.ask_with_deadline(provider, CLASSIFY_SYSTEM,
                                      [{"role": "user", "content": (message or "")[:6000]}],
                                      CLASSIFY_DEADLINE_SECONDS)
    except Exception:
        log.info("code-task check did not answer; the message stays in the chat", exc_info=True)
        return False
    words = re.findall(r"[A-Za-z]+", raw or "")
    return bool(words) and words[0].upper() == "CODE"


def _repos() -> list[dict]:
    from .sessions.store import SessionStore

    return [r for r in SessionStore().list_repos() if r.get("enabled", 1)]


def pick_repo(message: str) -> dict | None:
    """The marked repository the message is about: the only one, the one whose
    folder name the message mentions, or else the most recently marked."""
    repos = _repos()
    if not repos:
        return None
    if len(repos) == 1:
        return repos[0]
    text = (message or "").lower()
    named = [r for r in repos if os.path.basename(r["repo_path"].rstrip("/")).lower() in text]
    if named:
        return max(named, key=lambda r: len(r["repo_path"]))
    return max(repos, key=lambda r: int(r.get("updated_at") or r.get("created_at") or 0))


def _base_ref(repo_path: str) -> str:
    """``main``, else ``master``, else the branch the repository is on."""
    import subprocess

    def ok(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", repo_path, *args], capture_output=True, text=True,
                              timeout=15, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})

    for name in ("main", "master"):
        if ok("rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0:
            return name
    head = ok("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return head if head and head != "HEAD" else "main"


def _title(message: str) -> str:
    for line in (message or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:120]
    return "Task from the chat"


def start(message: str) -> dict:
    """Open a code session for ``message``, queue it as the task and start it.

    Returns ``{"ok": True, session_id, task_id, repo, repo_name, branch,
    base_ref, provider, model, flat_rate}`` or ``{"ok": False, "error",
    "message"}`` with a reason the chat can show.
    """
    from . import platform_caps
    from . import routes_sessions as rs
    from .sessions import providers

    if not platform_caps.code_sessions_supported():
        return {"ok": False, "error": "platform_unsupported",
                "message": "code sessions are not available on Windows yet"}
    repo = pick_repo(message)
    if repo is None:
        return {"ok": False, "error": "no_repo",
                "message": "no repository is marked for code sessions yet"}
    defaults = providers.session_defaults()
    if not defaults["provider"]:
        return {"ok": False, "error": "no_provider", "message": "no AI provider is set up yet"}
    repo_path = repo["repo_path"]
    base = _base_ref(repo_path)
    try:
        sid = rs.open_code_session(name="From the chat", role="engineer",
                                   provider=defaults["provider"], model=defaults["model"] or "",
                                   repo_path=repo_path, base_ref=base,
                                   budget_cost_cents=defaults["budget_cost_cents"],
                                   max_iterations=MAX_ITERATIONS)
    except Exception as e:  # ApiError, SessionValidationError, SessionLimitError…
        return {"ok": False, "error": "session_refused",
                "message": getattr(e, "message", None) or str(e)}
    store = rs._store()
    tid = store.enqueue_task(title=_title(message), prompt=message, assigned_session_id=sid,
                             created_by="chat")
    session = store.get_session(sid) or {}
    try:
        rs._preflight_provider(session)
        rs.supervisor().start(sid, stop_when_idle=True)
    except Exception as e:
        store.set_session_status(sid, "waiting_human", last_error=str(e)[:300])
        return {"ok": False, "error": "start_failed", "session_id": sid, "task_id": tid,
                "message": getattr(e, "message", None) or str(e)}
    return {"ok": True, "session_id": sid, "task_id": tid, "repo": repo_path,
            "repo_name": os.path.basename(repo_path.rstrip("/")), "branch": session.get("branch"),
            "base_ref": base, "provider": session.get("provider"), "model": session.get("model"),
            "flat_rate": providers.flat_rate(session.get("provider"))}
