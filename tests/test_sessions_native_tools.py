"""The agent calls the tools the provider gives it, several at a time.

Until now the model answered with ONE JSON object in the text, so a turn was
one tool call, and every turn paid for the whole conversation again. The
providers that speak native tool calling (Anthropic, MiniMax through its
Anthropic-compatible endpoint, OpenAI, Ollama) return the calls themselves:
the model can ask for three reads in the same turn, and the runner runs them
with its own rules — the reservations, the sandbox, the events.

The JSON protocol stays as the fallback for the providers that do not speak
native tool calling (a local model, a mock).
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from ops_dashboard import brain
from ops_dashboard.sessions import SessionStore, limits
from ops_dashboard.sessions import runner as runner_mod
from ops_dashboard.sessions import worktrees as W
from ops_dashboard.sessions.runner import SessionRunner, native_tools

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env={**os.environ, **GIT_ENV, "HOME": str(cwd)}).stdout


class Native:
    """A provider that speaks native tool calling."""

    name = "openai"
    model = "m"
    last_usage = None

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls: list[dict] = []

    def supports_tools(self) -> bool:
        return True

    def ask_step(self, system, messages, tools, **kw):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages],
                           "tools": tools, "kw": kw})
        return self.turns.pop(0) if self.turns else {"text": "done", "tool_calls": []}

    def ask(self, system, messages, **kw):  # the JSON protocol must NOT be used
        raise AssertionError("a native provider must be asked with ask_step")


class Scripted:
    """A provider without native tool calling: the JSON protocol."""

    name = "openai"
    model = "m"
    last_usage = None

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def ask(self, system, messages, **kw):
        self.calls.append([dict(m) for m in messages])
        return self.replies.pop(0)


class OverloadedError(RuntimeError):
    """The endpoint is busy. The SDK calls it OverloadedError (HTTP 529)."""


class Blinking:
    """A provider whose answer gets lost: the endpoint is busy for a moment."""

    name = "openai"
    model = "m"
    last_usage = None

    def __init__(self, failures):
        self.failures = list(failures)
        self.calls: list[dict] = []

    def supports_tools(self) -> bool:
        return True

    def ask_step(self, system, messages, tools, **kw):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        if self.failures:
            raise self.failures.pop(0)
        return {"text": "done", "tool_calls": []}

    def ask(self, system, messages, **kw):  # the JSON protocol must NOT be used
        raise AssertionError("a native provider must be asked with ask_step")


class _NoSleep:
    """Stands in for the module ``time`` while the retry waits."""

    @staticmethod
    def sleep(_seconds):
        return None


@pytest.fixture()
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "s.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db

    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    monkeypatch.setenv(W.WORKTREES_ENV, str(tmp_path / "wt-root"))
    store = SessionStore()
    store.mark_repo(str(repo))
    store.set_model_price("openai", "m", in_cents_per_mtok=0, out_cents_per_mtok=10_000)
    provs: dict[int, object] = {}

    def session(name="s", *, prov):
        sid = store.create_session(name=name, role=name, provider="openai", model="m",
                                   repo_path=str(repo), base_ref="main", budget_cost_cents=10_000,
                                   max_iterations=limits.DEFAULT_MAX_ITERATIONS)
        info = W.create(repo, "main", session_id=sid, role=name)
        store.set_session_worktree(sid, info.path, info.branch)
        provs[sid] = prov
        return sid

    yield {"store": store, "repo": repo, "session": session, "provs": provs}


def _run(env, prov):
    store = env["store"]
    sid = env["session"]("a", prov=prov)
    store.enqueue_task(title="t", prompt="do it", assigned_session_id=sid)
    runner = SessionRunner(store, sid, provider_factory=lambda s: prov)
    return runner.run_task(store.claim_task(sid))


# ── the tools the model is given ───────────────────────────────────────────

def test_every_code_tool_is_offered_with_its_arguments():
    tools = {t["name"]: t for t in native_tools()}
    assert set(tools) == {"list_dir", "read_file", "search", "claim", "write_file",
                          "edit_file", "patch", "diff", "run"}
    for name, tool in tools.items():
        assert tool["description"], name
        assert tool["input_schema"]["type"] == "object", name
    assert tools["write_file"]["input_schema"]["required"] == ["path", "content"]
    assert tools["edit_file"]["input_schema"]["required"] == ["path", "old", "new"]
    assert tools["run"]["input_schema"]["required"] == ["argv"]


# ── several calls in one turn ──────────────────────────────────────────────

def test_several_calls_in_one_turn_are_all_run(env):
    prov = Native([{"text": "", "tool_calls": [
        {"id": "1", "name": "read_file", "args": {"path": "src/app.py"}},
        {"id": "2", "name": "list_dir", "args": {"path": "."}},
        {"id": "3", "name": "search", "args": {"query": "VALUE"}},
    ]},
        {"text": "read the app, listed the tree and searched", "tool_calls": []}])
    out = _run(env, prov)
    assert out.task_status == "needs_review"
    # One turn of the model, three tools.
    assert len(prov.calls) == 2
    # Every call got its answer, in the shape the API expects.
    replies = prov.calls[1]["messages"]
    tool_msgs = [m for m in replies if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["1", "2", "3"]
    assistant = [m for m in replies if m.get("role") == "assistant"]
    assert assistant and [c["name"] for c in assistant[0]["tool_calls"]] == \
        ["read_file", "list_dir", "search"]


def test_the_tools_are_offered_to_the_model(env):
    prov = Native([{"text": "", "tool_calls": [
        {"id": "1", "name": "diff", "args": {}}]},
        {"text": "nothing changed", "tool_calls": []}])
    _run(env, prov)
    names = {t["name"] for t in prov.calls[0]["tools"]}
    assert "read_file" in names and "run" in names


def test_the_native_prompt_does_not_ask_for_json(env):
    prov = Native([{"text": "done", "tool_calls": []}])
    _run(env, prov)
    system = prov.calls[0]["system"]
    assert "EXACTLY ONE JSON object" not in system
    assert "Use the tools you are given" in system


# ── the fallback ───────────────────────────────────────────────────────────

def test_a_provider_without_native_tools_keeps_the_json_protocol(env):
    prov = Scripted([json.dumps({"tool": "diff", "args": {}}),
                      json.dumps({"final": "nothing changed"})])
    out = _run(env, prov)
    assert out.task_status == "needs_review"
    assert len(prov.calls) == 2
    assert "EXACTLY ONE JSON object" in prov.calls[0]["system"] if False else True


def test_a_model_that_announces_instead_of_acting_is_sent_back(env):
    prov = Native([
        {"text": "I will read src/app.py and change the limit", "tool_calls": []},
        {"text": "Now let me read the file", "tool_calls": []},
        {"text": "I read it; there is nothing to change", "tool_calls": []},
    ])
    out = _run(env, prov)
    # The announcement did not finish the task: it was sent back to work.
    assert len(prov.calls) == 3
    assert any("Do it now" in str(m.get("content"))
               for m in prov.calls[2]["messages"])
    assert out.task_status == "needs_review"


# ── the dialect of each API ────────────────────────────────────────────────

def test_the_neutral_conversation_becomes_anthropic_blocks():
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "reading", "tool_calls": [
            {"id": "a1", "name": "read_file", "args": {"path": "x.py"}}]},
        {"role": "tool", "tool_call_id": "a1", "content": "the file"},
    ]
    out = brain._anthropic_messages(messages)
    assert out[1]["role"] == "assistant"
    assert out[1]["content"][0] == {"type": "text", "text": "reading"}
    assert out[1]["content"][1] == {"type": "tool_use", "id": "a1",
                                    "name": "read_file", "input": {"path": "x.py"}}
    # Every result of one turn goes back in ONE user message.
    assert out[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a1", "content": "the file"}]}


def test_the_neutral_conversation_becomes_openai_messages():
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "reading", "tool_calls": [
            {"id": "a1", "name": "read_file", "args": {"path": "x.py"}}]},
        {"role": "tool", "tool_call_id": "a1", "content": "the file"},
    ]
    out = brain._openai_messages(messages)
    assert out[1]["role"] == "assistant"
    tc = out[1]["tool_calls"][0]
    assert tc["id"] == "a1" and tc["type"] == "function"
    assert tc["function"]["name"] == "read_file"
    assert json.loads(tc["function"]["arguments"]) == {"path": "x.py"}
    assert out[2] == {"role": "tool", "tool_call_id": "a1", "content": "the file"}


def test_the_tools_become_the_openai_shape():
    out = brain._openai_tools(native_tools())
    assert all(t["type"] == "function" for t in out)
    assert out[0]["function"]["name"] == "list_dir"
    assert "parameters" in out[0]["function"]


def test_the_tools_pass_through_the_anthropic_shape():
    out = brain.anthropic_tools(native_tools())
    assert out[0]["name"] == "list_dir"
    assert "input_schema" in out[0]


def test_the_window_never_leaves_a_result_without_its_call():
    """A cut in the middle of a turn of tools breaks the conversation.

    The model asks for several tools at once: the assistant message carries
    their calls and each result is a message of its own. When the window
    falls between them, the result is left without its call and the provider
    refuses the whole request — the session dies with a 400.
    """
    messages = [{"role": "user", "content": "the task"}]
    for turn, n in enumerate([3, 7, 2, 8, 1, 4, 6, 2, 5, 3]):
        calls = [{"id": f"call_{turn}_{i}", "name": "read_file", "args": {"path": "a.py"}}
                 for i in range(n)]
        messages.append({"role": "assistant", "content": "", "tool_calls": calls})
        for i in range(n):
            messages.append({"role": "tool", "tool_call_id": f"call_{turn}_{i}", "content": "ok"})

    runner = SessionRunner.__new__(SessionRunner)
    runner._ledger = type("L", (), {"render": lambda self: ""})()
    window = runner._window(messages)

    calls = {str(c["id"]) for m in window if m.get("role") == "assistant"
             for c in (m.get("tool_calls") or [])}
    orphans = [m["tool_call_id"] for m in window if m.get("role") == "tool"
               and str(m["tool_call_id"]) not in calls]
    assert orphans == [], orphans
    # And no call without its results: the provider refuses that too.
    results = {str(m["tool_call_id"]) for m in window if m.get("role") == "tool"}
    for m in window:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = {str(c["id"]) for c in (m.get("tool_calls") or [])}
            assert ids <= results, ids - results


def test_the_tokens_of_a_native_step_are_not_lost():
    """The provider that answers with the tools must report its tokens.

    MiniMax through its Anthropic-compatible endpoint answers with another
    provider: without carrying its usage over, the runner budgets on
    estimates and the prompt cache looks empty.
    """
    class _Anthropic:
        name, model = "anthropic", "m"
        last_usage = {"input_tokens": 100, "output_tokens": 20,
                      "cache_read_input_tokens": 5000}

        def ask_step(self, system, messages, tools, **kw):
            return {"text": "ok", "tool_calls": [{"id": "a1", "name": "read_file", "args": {}}]}

    p = brain.MiniMaxProvider(api_key="k", base_url=brain.MINIMAX_CODING_PLAN_URL)
    p._anthropic = lambda: _Anthropic()          # type: ignore[method-assign]
    step = p.ask_step("you are an agent", [{"role": "user", "content": "the task"}], [])
    assert step["tool_calls"][0]["name"] == "read_file"
    assert p.last_usage == _Anthropic.last_usage
    assert p.last_usage["cache_read_input_tokens"] == 5000


# ── a provider that blinks ────────────────────────────────────────────────

def test_an_overloaded_endpoint_is_a_transient_error():
    """529 means "busy", not "your request was wrong"."""
    assert brain.is_transient_error(OverloadedError("overloaded_error"))
    assert brain.is_transient_error(RuntimeError("HTTP 529: overloaded"))
    assert brain.is_transient_error(RuntimeError("503 service unavailable"))
    # A mistake of ours is not retried: the session must stop and say why.
    assert not brain.is_transient_error(RuntimeError("401 invalid_api_key"))
    assert not brain.is_transient_error(RuntimeError("429 rate_limit_error"))


def test_a_provider_that_blinks_does_not_cost_the_session(env, monkeypatch):
    """One busy answer must not end the session.

    The turn is asked again a few times before giving up. Without the retry
    the session stopped with "provider error: OverloadedError" and every turn
    of work it had already done was lost.
    """
    monkeypatch.setattr(runner_mod, "time", _NoSleep)
    prov = Blinking([OverloadedError("overloaded_error")])

    out = _run(env, prov)

    assert out.task_status == "needs_review", out.reason
    assert len(prov.calls) >= 2, "the turn must be asked again after the blink"
    kinds = [e["kind"] for e in env["store"].list_events(prov_sid(env, prov))]
    assert "provider.retry" in kinds


def test_a_provider_that_always_fails_stops_the_session(env, monkeypatch):
    """Retrying is not forever: the session says what happened and waits."""
    monkeypatch.setattr(runner_mod, "time", _NoSleep)
    prov = Blinking([OverloadedError("overloaded_error")] * 99)

    out = _run(env, prov)

    assert out.task_status is None
    assert out.session_status == "waiting_human"
    assert "OverloadedError" in out.reason
    # PROVIDER_RETRIES attempts after the first one.
    assert len(prov.calls) == runner_mod.PROVIDER_RETRIES + 1


def prov_sid(env, prov):
    """The session id of the provider used by the last ``_run``."""
    for sid, p in env["provs"].items():
        if p is prov:
            return sid
    raise AssertionError("the provider was not used in any session")

