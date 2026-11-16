"""A code session runs its task with Claude Code when the person has it.

On 16-nov-2026 the built-in runner spent 60 turns and changed nothing; the same
model solved the task inside Claude Code. These tests run the session with a
stand-in ``claude`` that speaks the same stream-json: the session's events,
its commit, the resume after a run that only announced its next step, and the
key, which must never travel in the environment.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_sessions_api import _task, _until, api  # noqa: F401

from ops_dashboard import brain, routes_sessions
from ops_dashboard.sessions import agent_cli
from ops_dashboard.sessions.browser_runner import runner_class_for
from ops_dashboard.sessions.runner import SessionRunner

KEY = "sk-cp-test-" + "k" * 40

FAKE = r'''#!PYTHON
import json, os, sys
dump = os.environ.get("FAKE_DUMP") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "dump.json")
args = sys.argv[1:]
settings = json.load(open(args[args.index("--settings") + 1]))
helper = settings.get("apiKeyHelper", "")
key_file = helper.split(" ", 1)[1] if helper.startswith("cat ") else ""
resumed = "--resume" in args
mode = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mode")).read().strip()
record = {"env": dict(os.environ), "args": args, "helper": helper, "key_file": key_file,
          "key_via_helper": open(key_file).read() if key_file else None, "cwd": os.getcwd(),
          "resumed": resumed}
runs = json.load(open(dump)) if os.path.exists(dump) else []
runs.append(record)
json.dump(runs, open(dump, "w"))
def emit(obj):
    print(json.dumps(obj), flush=True)
emit({"type": "system", "subtype": "init", "session_id": "agent-1", "model": "MiniMax-M3"})
if mode == "announce" and not resumed:
    emit({"type": "assistant", "message": {"usage": {"input_tokens": 10, "output_tokens": 5},
          "content": [{"type": "text", "text": "Now let me write the test first."}]}})
    emit({"type": "result", "subtype": "success", "result": "Now let me write the test first.",
          "num_turns": 1, "session_id": "agent-1"})
    sys.exit(0)
if mode != "nothing":
    with open("notes.txt", "w") as f:
        f.write("hello from the agent\n")
emit({"type": "assistant", "message": {"usage": {"input_tokens": 100, "output_tokens": 20},
      "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": os.path.join(os.getcwd(), "app.py")}},
                  {"type": "tool_use", "name": "Write", "input": {"file_path": os.path.join(os.getcwd(), "notes.txt")}}]}})
emit({"type": "assistant", "message": {"usage": {"input_tokens": 120, "output_tokens": 30},
      "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q tests"}}]}})
emit({"type": "result", "subtype": "success", "num_turns": 3, "session_id": "agent-1",
      "result": "Informe: añadí notes.txt; los tests pasan."})
'''


@pytest.fixture()
def claude(tmp_path, monkeypatch):
    """A stand-in ``claude`` and the chat set up with MiniMax's Coding Plan."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    exe = bindir / "claude"
    exe.write_text(FAKE.replace("#!PYTHON", "#!" + sys.executable))
    exe.chmod(0o755)
    (bindir / "mode").write_text("work")
    monkeypatch.setenv("CONDUCTLOOP_CLAUDE_BIN", str(exe))
    monkeypatch.setenv("MINIMAX_API_KEY", KEY)          # must not reach the agent
    cfg = {"default_provider": "minimax", "minimax_base_url": brain.MINIMAX_CODING_PLAN_URL}
    monkeypatch.setattr(brain, "_load_config", lambda: dict(cfg))
    return SimpleNamespace(dir=bindir, cfg=cfg, dump=bindir / "dump.json",
                           mode=lambda m: (bindir / "mode").write_text(m),
                           runs=lambda: json.loads((bindir / "dump.json").read_text()))


def _session(api):  # noqa: F811
    call = api["call"]
    call("POST", "/api/sessions/repos", {"path": str(api["repo"])})
    api["default"]["p"] = SimpleNamespace(api_key=KEY, model="MiniMax-M3", name="minimax")
    st, r = call("POST", "/api/sessions", {"repo_path": str(api["repo"]), "base_ref": "main"})
    assert st == 201, r
    sid = r["session"]["id"]
    tid = call("POST", "/api/sessions/tasks", {"session_id": sid, "title": "notes",
                                               "prompt": "add notes.txt"})[1]["task"]["id"]
    return sid, tid


def _events(api, sid):  # noqa: F811
    return api["call"]("GET", f"/api/sessions/{sid}/events?limit=500")[1]["items"]


def test_a_code_session_runs_its_task_with_claude_code(api, claude):  # noqa: F811
    call = api["call"]
    sid, tid = _session(api)
    assert runner_class_for(routes_sessions._store(), sid) is agent_cli.AgentCliRunner
    call("POST", f"/api/sessions/{sid}/start")
    _until(lambda: _task(api, tid)["status"] == "needs_review")
    task = _task(api, tid)
    assert task["result_summary"] == "Informe: añadí notes.txt; los tests pasan."
    kinds = [e["kind"] for e in _events(api, sid)]
    assert "tool.write" in kinds and "tool.read" in kinds and "tool.run" in kinds
    assert "integration.dry_merge" in kinds and kinds.count("llm.turn") == 2
    write = next(e for e in _events(api, sid) if e["kind"] == "tool.write")
    payload = write["payload"] if isinstance(write["payload"], dict) else json.loads(write["payload"])
    assert payload["path"] == "notes.txt"                     # relative to the worktree
    s = call("GET", f"/api/sessions/{sid}")[1]["session"]
    log = subprocess.run(["git", "-C", s["worktree_path"], "log", "-1", "--name-only", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "notes.txt" in log and f"task #{tid}" in log


def test_the_key_never_travels_in_the_environment(api, claude):  # noqa: F811
    sid, tid = _session(api)
    api["call"]("POST", f"/api/sessions/{sid}/start")
    _until(lambda: _task(api, tid)["status"] == "needs_review")
    run = claude.runs()[0]
    assert KEY not in json.dumps(run["env"]) and KEY not in json.dumps(run["args"])
    assert run["key_via_helper"] == KEY                       # Claude Code gets it through the helper
    assert run["env"]["ANTHROPIC_BASE_URL"] == brain.MINIMAX_CODING_PLAN_URL
    assert run["env"]["ANTHROPIC_MODEL"] == "MiniMax-M3"
    assert run["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == agent_cli.MAX_OUTPUT_TOKENS
    assert "MINIMAX_API_KEY" not in run["env"] and "ANTHROPIC_API_KEY" not in run["env"]
    assert "--bare" in run["args"] and "acceptEdits" in run["args"]
    assert not any(a.startswith("Bash(git push") or a.startswith("Bash(git commit") for a in run["args"])
    assert not os.path.exists(run["key_file"])                # the private folder is gone


def test_a_run_that_only_announces_is_resumed(api, claude):  # noqa: F811
    claude.mode("announce")
    sid, tid = _session(api)
    api["call"]("POST", f"/api/sessions/{sid}/start")
    _until(lambda: _task(api, tid)["status"] == "needs_review")
    runs = claude.runs()
    assert [r["resumed"] for r in runs] == [False, True]
    assert runs[1]["args"][runs[1]["args"].index("--resume") + 1] == "agent-1"
    assert "agent.resumed" in [e["kind"] for e in _events(api, sid)]


def test_a_run_that_changes_nothing_is_blocked(api, claude):  # noqa: F811
    claude.mode("nothing")
    sid, tid = _session(api)
    api["call"]("POST", f"/api/sessions/{sid}/start")
    _until(lambda: _task(api, tid)["status"] == "blocked")
    assert "Informe" in _task(api, tid)["result_summary"]


def test_without_claude_or_with_another_provider_the_built_in_runner_works(claude, monkeypatch, tmp_path):
    s = {"provider": "minimax", "worktree_path": str(tmp_path), "browser_mode": "off"}
    assert agent_cli.should_use(s)
    assert not agent_cli.should_use({**s, "provider": "openai"})
    assert not agent_cli.should_use({**s, "browser_mode": "read"})
    claude.cfg["minimax_base_url"] = "https://api.minimaxi.chat/v1"     # pay-as-you-go, not Anthropic's API
    assert not agent_cli.should_use(s)
    claude.cfg["minimax_base_url"] = brain.MINIMAX_CODING_PLAN_URL
    claude.cfg["code_agent"] = "builtin"
    assert not agent_cli.should_use(s)
    del claude.cfg["code_agent"]
    monkeypatch.setenv("CONDUCTLOOP_CLAUDE_BIN", str(tmp_path / "nope"))
    assert not agent_cli.should_use(s)
    assert agent_cli.should_use({**s, "provider": "anthropic"}) is False      # no binary either way


def test_the_announcement_pattern():
    for text in ("Now let me write the test first.", "I'll write the P1 guardian test now",
                 "Let me check the file", "Ahora voy a escribir el test", "Voy a revisar"):
        assert agent_cli._ANNOUNCES.match(text), text
    for text in ("Informe: añadí notes.txt", "Done. P1 and P2 are fixed.", "## P1 · README"):
        assert not agent_cli._ANNOUNCES.match(text), text


def test_the_built_in_runner_is_still_the_default_without_claude(api, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("CONDUCTLOOP_CLAUDE_BIN", str(tmp_path / "nope"))
    cfg = {"default_provider": "minimax", "minimax_base_url": brain.MINIMAX_CODING_PLAN_URL}
    monkeypatch.setattr(brain, "_load_config", lambda: dict(cfg))
    call = api["call"]
    call("POST", "/api/sessions/repos", {"path": str(api["repo"])})
    sid = call("POST", "/api/sessions", {"repo_path": str(api["repo"]), "base_ref": "main"})[1]["session"]["id"]
    assert runner_class_for(routes_sessions._store(), sid) is SessionRunner
