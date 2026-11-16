#!/usr/bin/env python3
"""The agent exam: can the built-in runner do real work?

On 16-nov-2026 the built-in session runner (``sessions/runner.py``) spent 60
turns of a real task reading and searching, burned 657k input tokens and
changed nothing. The same model (MiniMax-M3) solved the same task in 15
minutes inside Claude Code. The model was fine; the runner was not.

Measuring first, changing second. This script runs a task end to end against
a throwaway copy of the repository and says what happened:

* a **temporary clone** of the repository, **without remotes**, at the base
  commit of the task — never the working tree, never a push;
* a **temporary database** — ``~/ops-dashboard/data/ops.db`` is never opened;
* the **session's own provider** (the one the person configured: MiniMax-M3
  through the Coding Plan by default), so the numbers are the numbers of the
  real thing;
* the **hidden tests** of the task, applied to the agent's worktree
  afterwards: they are the ones that decide whether the task was done.

Usage::

    scripts/agent_exam.py --runner builtin --runner claude
    scripts/agent_exam.py --runner builtin --task A --out /tmp/exam.json
    scripts/agent_exam.py --dry-run          # the plumbing, with a scripted model

It spends the owner's Coding Plan quota (a flat rate): it is a bench, not a
test, and it never runs in CI.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The code under test is THIS checkout, not whatever is installed in the
# environment: a session runner that is not the one being measured is a
# measurement of nothing. The repository root goes first on sys.path so the
# package here wins over an editable install pointing somewhere else.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

EXAM_DIR = REPO_ROOT / "docs" / "evals" / "agent-exam"
DEFAULT_MAX_ITERATIONS = 120
DEFAULT_BUDGET_CENTS = 5000
DEFAULT_TIMEOUT_MIN = 60


# ── small helpers ──────────────────────────────────────────────────────────

def _git(*args: str, cwd: str | Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def _git_common_dir() -> Path:
    """The bare repository behind this checkout (the one with every commit)."""
    out = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=REPO_ROOT)
    return Path(out)


# ── the tasks ──────────────────────────────────────────────────────────────

@dataclass
class ExamTask:
    key: str
    name: str
    folder: Path
    prompt: str
    base: str
    hidden: list[Path] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.folder.name


def load_tasks(only: list[str] | None = None) -> list[ExamTask]:
    tasks: list[ExamTask] = []
    if not EXAM_DIR.is_dir():
        return tasks
    for folder in sorted(EXAM_DIR.iterdir()):
        if not folder.is_dir():
            continue
        key = folder.name.split("-", 1)[0].upper()
        if only and key not in only:
            continue
        task_file = folder / "task.md"
        base_file = folder / "base"
        if not task_file.is_file() or not base_file.is_file():
            continue
        hidden = sorted(str(p.relative_to(folder)) for p in
                        (folder / "hidden_tests").rglob("test_*.py"))
        tasks.append(ExamTask(
            key=key, name=folder.name, folder=folder,
            prompt=task_file.read_text(encoding="utf-8"),
            base=base_file.read_text(encoding="utf-8").strip(),
            hidden=hidden,
        ))
    return tasks


# ── the clone ──────────────────────────────────────────────────────────────

def make_clone(source: Path, base: str, dest: Path) -> str:
    """A clone without remotes, at ``base``. Returns the branch name."""
    _git("clone", "--no-hardlinks", "--quiet", str(source), str(dest))
    # No remotes: the agent must not be able to push anywhere.
    _git("remote", "remove", "origin", cwd=dest, check=False)
    for extra in _git("remote", cwd=dest).splitlines():
        _git("remote", "remove", extra, cwd=dest, check=False)
    branch = "exam-base"
    _git("checkout", "--quiet", "-B", branch, base, cwd=dest)
    return branch


# ── the session ────────────────────────────────────────────────────────────

def open_session(task: ExamTask, clone: Path, branch: str, tmp: Path,
                 max_iterations: int) -> tuple[Any, int, Path]:
    """A code session on the clone, with its own worktree. Returns the store,
    the session id and the worktree path."""
    from ops_dashboard import db
    from ops_dashboard.sessions import providers, worktrees
    from ops_dashboard.sessions import store as store_mod

    # The real database is never opened: this one lives in the temp folder.
    db.DB_PATH = tmp / "db" / "ops.db"
    db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(backup_before_upgrade=False)

    st = store_mod.SessionStore()
    repo = st.mark_repo(str(clone), project_key="exam")

    defaults = providers.session_defaults()
    provider = defaults.get("provider") or "minimax"
    model = defaults.get("model") or providers.default_model(provider)
    if not model:
        raise RuntimeError("no model for the provider")

    # The cap of the app, not a number of this script: the exam measures the
    # runner as it ships.
    from ops_dashboard.sessions import limits
    iterations = max_iterations or limits.DEFAULT_MAX_ITERATIONS

    sid = st.create_session(
        name=f"Exam {task.key}", role="engineer", provider=provider, model=model,
        repo_path=repo, base_ref=branch, budget_cost_cents=DEFAULT_BUDGET_CENTS,
        max_iterations=iterations,
    )
    info = worktrees.create(clone, branch, session_id=sid, role="engineer",
                            root=tmp / "worktrees")
    st.set_session_worktree(sid, info.path, info.branch)
    return st, sid, Path(info.path)


# ── the run ────────────────────────────────────────────────────────────────

class _ScriptedProvider:
    """A provider that says one thing. Only for ``--dry-run``."""

    name = "mock"
    last_usage = None
    model = "scripted-1"

    def ask(self, system: str, messages: list[dict], **kwargs) -> str:
        return json.dumps({"final": "dry run: nothing was changed"})


def run_task(runner_name: str, st: Any, sid: int, task_row: dict, *,
             timeout_s: int, dry_run: bool, worktrees_root: Path | None = None) -> dict:
    """Run one task with one runner. Returns the outcome and the reason."""
    from ops_dashboard.sessions.agent_cli import AgentCliRunner
    from ops_dashboard.sessions.runner import SessionRunner

    cancel = threading.Event()
    tools_kwargs = {"python": sys.executable}
    if worktrees_root:
        tools_kwargs["worktrees_root"] = str(worktrees_root)
    if runner_name == "claude":
        runner = AgentCliRunner(st, sid, cancel=cancel, tools_kwargs=tools_kwargs)
    else:
        factory = (lambda s: _ScriptedProvider()) if dry_run else None
        runner = SessionRunner(st, sid, cancel=cancel, tools_kwargs=tools_kwargs,
                               **({"provider_factory": factory} if factory else {}))

    box: dict[str, Any] = {}

    def go() -> None:
        try:
            box["outcome"] = runner.run_task(task_row)
        except Exception as e:  # noqa: BLE001 - the exam reports, it does not crash
            box["error"] = f"{type(e).__name__}: {e}"[:300]

    thread = threading.Thread(target=go, name=f"exam-{runner_name}", daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        cancel.set()
        thread.join(120)
        box["timeout"] = True
    box["minutes"] = round((time.monotonic() - started) / 60.0, 2)
    return box


# ── the hidden tests ───────────────────────────────────────────────────────

def apply_and_run_hidden_tests(task: ExamTask, worktree: Path, python: str) -> dict:
    """Copy the hidden tests into the worktree and run them there."""
    if not task.hidden:
        return {"ran": False, "passed": False, "reason": "no hidden tests"}
    targets = []
    for rel in task.hidden:
        # rel is "hidden_tests/tests/test_x.py": the source in the exam folder,
        # the destination inside the agent's worktree.
        src = task.folder / rel
        dst = worktree / Path(rel).relative_to("hidden_tests")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        targets.append(str(dst))

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(worktree)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    proc = subprocess.run(
        [python, "-m", "pytest", "-p", "no:cacheprovider", "--rootdir", str(worktree),
         "-q", *targets],
        cwd=str(worktree), env=env, capture_output=True, text=True, timeout=1800)
    tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-1000:]
    return {"ran": True, "passed": proc.returncode == 0, "exit_code": proc.returncode,
            "output": tail}


# ── one exam run ───────────────────────────────────────────────────────────

def run_exam(task: ExamTask, runner_name: str, args: argparse.Namespace) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"cl-exam-{task.key}-"))
    row: dict[str, Any] = {
        "runner": runner_name, "task": task.key, "task_name": task.name,
        "finished": False, "hidden_tests": False, "turns": 0,
        "tokens_in": 0, "tokens_out": 0, "minutes": 0.0, "error": "",
        "session_status": "", "task_status": "",
    }
    try:
        source = Path(args.source_repo) if args.source_repo else _git_common_dir()
        clone = tmp / "repo"
        branch = make_clone(source, task.base, clone)
        row["base"] = task.base

        st, sid, worktree = open_session(task, clone, branch, tmp, args.max_iterations)
        task_id = st.enqueue_task(title=f"Exam {task.key}", prompt=task.prompt,
                                  assigned_session_id=sid, created_by="exam")
        claimed = st.claim_task(sid, task_id)
        if claimed is None:
            row["error"] = "the session could not claim its own task"
            return row

        box = run_task(runner_name, st, sid, claimed,
                       timeout_s=args.timeout_minutes * 60, dry_run=args.dry_run,
                       worktrees_root=tmp / "worktrees")
        outcome = box.get("outcome")
        row["minutes"] = box.get("minutes", 0.0)
        if box.get("error"):
            row["error"] = box["error"]
        if box.get("timeout"):
            row["error"] = (row["error"] + " " if row["error"] else "") + "timed out"
        if outcome is not None:
            row["session_status"] = outcome.session_status
            row["task_status"] = outcome.task_status or ""
            row["reason"] = outcome.reason
            row["finished"] = outcome.task_status == "needs_review"
            row["iterations"] = outcome.iterations

        # The turns and the tokens the run really spent.
        for ev in st.list_events(sid, limit=1000):
            if ev["kind"] == "llm.turn":
                row["turns"] += 1
                row["tokens_in"] += int(ev["tokens_in"] or 0)
                row["tokens_out"] += int(ev["tokens_out"] or 0)

        # What the agent actually changed — before the hidden tests land on top.
        changed = _git("status", "--porcelain=v1", "--untracked-files=all", cwd=worktree)
        row["changed_files"] = [c[3:] for c in changed.splitlines() if len(c) > 3]

        hidden = apply_and_run_hidden_tests(task, worktree, sys.executable)
        row["hidden_tests"] = bool(hidden.get("passed"))
        row["hidden_detail"] = {k: v for k, v in hidden.items() if k != "output"}
        if hidden.get("output"):
            row["hidden_output"] = hidden["output"][-1200:]
    except Exception as e:  # noqa: BLE001 - the exam reports, it does not crash
        row["error"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        if args.keep:
            row["kept"] = str(tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return row


# ── the table ──────────────────────────────────────────────────────────────

def print_table(rows: list[dict]) -> None:
    head = ["runner", "task", "finished", "hidden tests", "turns", "tokens in",
            "tokens out", "min"]
    print()
    print("| " + " | ".join(head) + " |")
    print("|" + "|".join("---" for _ in head) + "|")
    for r in rows:
        print("| " + " | ".join([
            str(r.get("runner", "")), str(r.get("task", "")),
            "yes" if r.get("finished") else "no",
            "yes" if r.get("hidden_tests") else "no",
            str(r.get("turns", 0)), f"{r.get('tokens_in', 0):,}",
            f"{r.get('tokens_out', 0):,}", f"{r.get('minutes', 0)}",
        ]) + " |")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="The agent exam.")
    ap.add_argument("--runner", action="append", default=[],
                    choices=["builtin", "claude"], help="the runner to measure")
    ap.add_argument("--task", action="append", default=[],
                    help="only this task (A, B…); default: all")
    ap.add_argument("--out", help="write the raw results as JSON here")
    ap.add_argument("--source-repo", help="the repository to clone (default: this one)")
    ap.add_argument("--max-iterations", type=int, default=0,
                    help="0 (default): the cap the app ships with")
    ap.add_argument("--timeout-minutes", type=int, default=DEFAULT_TIMEOUT_MIN)
    ap.add_argument("--keep", action="store_true", help="keep the temporary folders")
    ap.add_argument("--dry-run", action="store_true",
                    help="exercise the plumbing with a scripted model (no quota)")
    args = ap.parse_args()

    runners = args.runner or ["builtin"]
    tasks = load_tasks(args.task or None)
    if not tasks:
        print(f"no exam task found in {EXAM_DIR}")
        return 2
    if "claude" in runners and args.dry_run:
        runners = [r for r in runners if r != "claude"]

    rows: list[dict] = []
    for runner_name in runners:
        for task in tasks:
            print(f"· {runner_name} · task {task.key} ({task.name}) …", flush=True)
            row = run_exam(task, runner_name, args)
            rows.append(row)
            print(f"  finished={row['finished']} hidden={row['hidden_tests']} "
                  f"turns={row['turns']} in={row['tokens_in']} out={row['tokens_out']} "
                  f"{row['minutes']}min {row.get('error', '')}", flush=True)

    print_table(rows)
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"raw results: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
