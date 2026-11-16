"""Run a code session's task with a coding agent the person already has.

The built-in session runner (``runner.SessionRunner``) drives the model one
tool call per turn, through a small whitelist, resending the whole
conversation every time. On 16-nov-2026 it spent its 60 turns reading and
searching and changed nothing; the same model, MiniMax-M3, solved the same task
in 15 minutes inside Claude Code. Writing a harness as good as the ones that
exist is not the work of this app; running them is.

So when the person has the Claude Code CLI (``claude``) and the session's
provider speaks Anthropic's API (Anthropic itself, or MiniMax through its
Coding Plan endpoint), a code session runs its task with it:

- in the session's own worktree, on its ``cl/…`` branch, as before;
- headless (``claude --bare -p``: no hooks, no CLAUDE.md, no memory of the
  person's own Claude Code), streaming JSON that becomes the session's events,
  so the chat card follows it as it follows the built-in runner;
- with the session's model and key. The key goes through ``apiKeyHelper``
  (a 0600 file in a private temporary folder, removed when the run ends),
  never through the environment: the commands the agent runs inherit the
  environment, and on 16-nov two tests failed because they saw the key;
- with an environment built from an allow-list, and Claude Code's own
  permissions: edits accepted inside the worktree, a short list of commands
  (tests, lint, read-only git); no commit and no push. The runner commits the
  changed files itself, like the built-in one, and the person pushes them.

What it does not have is the built-in runner's OS sandbox: the agent needs the
network to reach the model. Its limits are Claude Code's permissions above.

A run that ends announcing its next step ("Now let me write…") instead of
taking it is resumed with a plain "continue", twice at most: some models end
their turn there. Without ``claude``, or with another provider, or with
``code_agent: builtin`` in the configuration, sessions use the built-in runner.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import worktrees
from .runner import RunOutcome, SessionRunner

# How long one task may run, and how often a resumed run may be asked to go on.
RUN_TIMEOUT_S = 45 * 60
MAX_RESUMES = 2

# Claude Code does not know MiniMax-M3 and gives it a small output budget: the
# model wrote "Now let me write the test" and ended its turn, twice, until the
# budget was raised. Reasoning counts against it, so it is bounded too.
MAX_OUTPUT_TOKENS = "32000"
MAX_THINKING_TOKENS = "4000"

ALLOWED_TOOLS = (
    "Read", "Edit", "MultiEdit", "Write", "Glob", "Grep", "TodoWrite",
    "Bash(pytest:*)", "Bash(python:*)", "Bash(python3:*)", "Bash(ruff:*)", "Bash(mypy:*)",
    "Bash(npm test:*)", "Bash(npm run:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(ls:*)",
)

PREFACE = (
    "You work in a copy of the repository (the current directory), on your own branch. "
    "Read, search, edit, and run the tests and the linter. Do not commit and do not push: "
    "when you finish, your changes are committed for you and a person reviews them. "
    "Finish with a short report, in the language the task is written in: what you changed, "
    "which tests you ran, and anything you could not do.\n\n"
)
CONTINUE = ("Continue the task until it is finished: make the changes, run the tests and the "
            "linter, and end with the report. Do not stop to announce what you are going to do; "
            "do it.")

# A run that ends on an announcement instead of a report.
_ANNOUNCES = re.compile(
    r"^\s*(?:ok[,.]?\s*|now[,]?\s*|next[,]?\s*|ahora[,]?\s*)?"
    r"(?:let me|let's|i'?ll|i will|i'm going to|i am going to|voy a|vamos a|primero voy)\b",
    re.I)

_ENV_KEEP = ("HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "SHELL", "TERM", "TZ")
_BIN_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.claude/local")


def _config() -> dict:
    from .. import brain

    try:
        return brain._load_config() or {}
    except Exception:
        return {}


def find_claude() -> str | None:
    """The ``claude`` binary: ``CONDUCTLOOP_CLAUDE_BIN``, the ``claude_bin``
    setting, or the usual places (the app does not inherit a shell's PATH)."""
    explicit = os.environ.get("CONDUCTLOOP_CLAUDE_BIN") or _config().get("claude_bin")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    path = os.pathsep.join([os.environ.get("PATH", "")] + [os.path.expanduser(d) for d in _BIN_DIRS])
    return shutil.which("claude", path=path)


def _base_url(provider: str) -> str | None | bool:
    """The Anthropic-compatible base URL of ``provider``: None for Anthropic
    itself, a URL for MiniMax's Coding Plan, False when it does not speak
    Anthropic's API."""
    if provider == "anthropic":
        return None
    if provider == "minimax":
        cfg = _config()
        base = (os.environ.get("MINIMAX_BASE_URL") or cfg.get("minimax_base_url") or "").rstrip("/")
        return base if base.endswith("/anthropic") else False
    return False


def should_use(session: dict) -> bool:
    """Whether this code session runs its tasks with Claude Code."""
    if str(session.get("browser_mode") or "off") != "off" or not session.get("worktree_path"):
        return False
    if str(_config().get("code_agent") or "auto").lower() == "builtin":
        return False
    if _base_url(str(session.get("provider") or "").lower()) is False:
        return False
    return find_claude() is not None


def _env(base_url: str | None, model: str, repo_path: str, config_dir: str) -> dict:
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env.update({k: v for k, v in os.environ.items() if k.startswith("LC_")})
    dirs = [str(Path(repo_path) / ".venv" / "bin"), str(Path(repo_path) / "node_modules" / ".bin")]
    dirs += [os.path.expanduser(d) for d in _BIN_DIRS] + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    env["PATH"] = os.pathsep.join(d for d in dirs if os.path.isdir(d))
    env.update({
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": model,
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": MAX_OUTPUT_TOKENS,
        "MAX_THINKING_TOKENS": MAX_THINKING_TOKENS,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "API_TIMEOUT_MS": "600000",
        "CLAUDE_CONFIG_DIR": config_dir,
    })
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    return env


def _rel(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root) if os.path.isabs(path) else path
    except ValueError:
        return path


class AgentCliRunner(SessionRunner):
    """A ``SessionRunner`` whose turns are taken by Claude Code."""

    def run_task(self, task: dict) -> RunOutcome:
        tid = task["id"]
        self.store.set_task_status(tid, self.session_id, "running")
        self.store.set_session_status(self.session_id, "running")
        self._event("task.started", {"title": task["title"], "agent": "claude-code"}, task_id=tid)
        s = self._session()
        if not self._price_ok(s, task):
            return self._stop(task, session_status="waiting_human", reason="no price for this model",
                              task_status=None, iterations=0)
        binary = find_claude()
        base = _base_url(str(s["provider"]).lower())
        if binary is None or base is False:
            return self._stop(task, session_status="waiting_human", task_status="blocked",
                              reason="Claude Code is not available for this session", iterations=0)
        try:
            provider = self.provider_factory(s)
        except Exception as e:
            return self._stop(task, session_status="waiting_human", task_status=None,
                              reason=f"provider: {e}"[:300], iterations=0)
        key = getattr(provider, "api_key", None)
        if not key:
            return self._stop(task, session_status="waiting_human", task_status=None,
                              reason="no key for this provider", iterations=0)
        model = s["model"] or getattr(provider, "model", "") or ""
        hints = json.loads(task.get("paths_hint") or "[]")
        prompt = (PREFACE + f"Task #{tid}: {task['title']}\n\n{task['prompt']}"
                  + (f"\n\nExpected paths: {', '.join(hints)}" if hints else ""))

        private = tempfile.mkdtemp(prefix="cl-agent-")
        os.chmod(private, 0o700)
        try:
            key_file = Path(private) / "key"
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key)
            settings = Path(private) / "settings.json"
            settings.write_text(json.dumps({"apiKeyHelper": f"cat {key_file}"}))
            env = _env(base or None, model, s["repo_path"], str(Path(private) / "config"))
            os.makedirs(env["CLAUDE_CONFIG_DIR"], mode=0o700, exist_ok=True)
            base_cmd = [binary, "--bare", "--model", model, "--output-format", "stream-json",
                        "--verbose", "--permission-mode", "acceptEdits", "--settings", str(settings),
                        "--allowedTools", *ALLOWED_TOOLS]
            report, turns, agent_session = "", 0, None
            for attempt in range(MAX_RESUMES + 1):
                cmd = list(base_cmd)
                if attempt == 0:
                    cmd += ["-p", prompt]
                else:
                    cmd += ["--resume", agent_session or "", "-p", CONTINUE]
                    self._event("agent.resumed", {"attempt": attempt}, task_id=tid)
                result = self._run(cmd, s["worktree_path"], env, task)
                if result.get("stopped"):
                    return result["stopped"]
                report = result.get("report") or report
                turns += int(result.get("turns") or 0)
                agent_session = result.get("session_id") or agent_session
                if not (agent_session and _ANNOUNCES.match(report or "")):
                    break
        finally:
            shutil.rmtree(private, ignore_errors=True)

        try:
            info = worktrees.inspect(s["repo_path"], s["worktree_path"], s["base_ref"])
        except worktrees.WorktreeError as e:
            return self._stop(task, session_status="waiting_human", task_status="blocked",
                              reason=f"worktree: {e}"[:300], iterations=turns)
        self._written = set(info.changed)
        if not self._written and not info.ahead:
            return self._stop(task, session_status="waiting_human", task_status="blocked",
                              reason="the agent changed nothing", iterations=turns,
                              summary=report or "the agent changed nothing")
        return self._finish(task, SimpleNamespace(root=Path(s["worktree_path"])),
                            report or "done", turns)

    # ── one run of the CLI ───────────────────────────────────────────────
    def _run(self, cmd: list[str], cwd: str, env: dict, task: dict) -> dict:
        tid = task["id"]
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, bufsize=1, start_new_session=True)
        except OSError as e:
            return {"stopped": self._stop(task, session_status="waiting_human", task_status="blocked",
                                          reason=f"could not start Claude Code: {e}"[:300],
                                          iterations=0)}
        deadline = time.monotonic() + RUN_TIMEOUT_S
        out: dict[str, Any] = {}
        stop = threading.Event()

        def watchdog() -> None:
            while not stop.wait(5):
                self.store.heartbeat(self.session_id)
                if self.cancel.is_set() or time.monotonic() > deadline:
                    _kill(proc)
                    return

        threading.Thread(target=watchdog, daemon=True, name=f"cl-agent-{self.session_id}").start()
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                self._consume(line, cwd, tid, out)
        finally:
            stop.set()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill(proc)
        if self.cancel.is_set():
            out["stopped"] = self._stop(task, session_status="idle", reason="cancelled",
                                        task_status="queued", iterations=int(out.get("turns") or 0))
        elif time.monotonic() > deadline:
            out["stopped"] = self._stop(task, session_status="waiting_human", task_status="blocked",
                                        reason=f"Claude Code ran for more than {RUN_TIMEOUT_S // 60} min",
                                        iterations=int(out.get("turns") or 0))
        elif proc.returncode not in (0, None) and not out.get("report"):
            out["stopped"] = self._stop(task, session_status="waiting_human", task_status="blocked",
                                        reason=f"Claude Code exited with {proc.returncode}",
                                        iterations=int(out.get("turns") or 0))
        return out

    def _consume(self, line: str, root: str, tid: int, out: dict) -> None:
        try:
            ev = json.loads(line)
        except ValueError:
            return
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            out["session_id"] = ev.get("session_id")
        elif kind == "assistant":
            msg = ev.get("message") or {}
            usage = msg.get("usage") or {}
            s = self._session()
            self.store.record_event(
                self.session_id, "llm.turn", {"agent": "claude-code"}, task_id=tid,
                tokens_in=int(usage.get("input_tokens") or 0),
                tokens_out=int(usage.get("output_tokens") or 0),
                iteration=int(s["iterations"]) < int(s["max_iterations"]))
            for c in msg.get("content") or []:
                if c.get("type") == "tool_use":
                    self._tool_event(c.get("name") or "", c.get("input") or {}, root, tid)
        elif kind == "result":
            out["report"] = str(ev.get("result") or "").strip()
            out["turns"] = ev.get("num_turns") or 0
            out["session_id"] = ev.get("session_id") or out.get("session_id")

    def _tool_event(self, name: str, args: dict, root: str, tid: int) -> None:
        path = args.get("file_path") or args.get("path") or args.get("notebook_path")
        if name in ("Edit", "MultiEdit", "NotebookEdit"):
            self._event("tool.edit", {"ok": True, "path": _rel(str(path or ""), root)}, task_id=tid)
        elif name == "Write":
            self._event("tool.write", {"ok": True, "path": _rel(str(path or ""), root)}, task_id=tid)
        elif name == "Read":
            self._event("tool.read", {"ok": True, "path": _rel(str(path or ""), root)}, task_id=tid)
        elif name in ("Grep", "Glob"):
            self._event("tool.search", {"ok": True}, task_id=tid)
        elif name == "Bash":
            self._event("tool.run", {"ok": True, "argv": [str(args.get("command") or "")[:80]]},
                        task_id=tid)


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, 15)
    except (OSError, ProcessLookupError):
        pass
