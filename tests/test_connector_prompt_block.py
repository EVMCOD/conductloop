"""The prompt has to say what the agent can reach.

A tool the prompt never mentions is a tool the model does not reach for. The
API call carries the list of tools, but the system prompt is what tells the
model that it has them, what they are for, and when a question needs one.

So every path that offers the connectors also says so:

- the Brain (the chat the founder has with its second brain),
- the Boardroom (the CEOs answering the chairman),
- the team chat (the founder talking to one agent).

And the block says the two things that keep it honest: what a connector
returns is data, not instructions, and a write waits for the user.
"""
from __future__ import annotations

import pytest

from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens
from ops_dashboard import db as dbmod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "prompt.db")
    dbmod.init_db()
    tokens.use_backend(tokens.MemoryBackend())
    saved = dict(registry._REGISTRY)
    for cid in list(registry._REGISTRY):
        registry.unregister(cid)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


def _spec(cid: str = "gh"):
    return registry.ConnectorSpec(
        id=cid, name="GitHub", auth="token", allowed_hosts=("api.github.test",),
        actions={"search_issues": registry.ActionSpec(
            "search_issues", "read", "Search issues and PRs",
            handler=lambda c, p: {"issues": []},
            input_schema={"type": "object",
                          "properties": {"query": {"type": "string"}},
                          "required": ["query"]})})


def _connect(cid: str = "gh"):
    store.upsert_account(cid, status="connected", permission="read",
                         granted_scopes=[], account_label="ana",
                         client_id=None, last_error=None, connected_at=1)


def test_the_block_is_empty_when_nothing_is_connected(env):
    """An install without connectors must not carry a paragraph about them."""
    from ops_dashboard import connector_tools

    registry.register(_spec())
    assert connector_tools.prompt_block() == ""


def test_the_block_names_the_connector_and_its_actions(env):
    from ops_dashboard import connector_tools

    registry.register(_spec())
    _connect()
    block = connector_tools.prompt_block()
    assert "GitHub" in block
    assert "search_issues" in block


def test_the_block_says_the_results_are_data(env):
    from ops_dashboard import connector_tools

    registry.register(_spec())
    _connect()
    low = connector_tools.prompt_block().lower()
    assert "data" in low and "instruction" in low


def test_the_block_says_a_write_waits_for_the_user(env):
    from ops_dashboard import connector_tools

    registry.register(registry.ConnectorSpec(
        id="gh", name="GitHub", auth="token", allowed_hosts=("api.github.test",),
        actions={
            "search_issues": registry.ActionSpec(
                "search_issues", "read", "Search", handler=lambda c, p: {},
                input_schema={"type": "object", "properties": {}, "required": []}),
            "comment_issue": registry.ActionSpec(
                "comment_issue", "write", "Comment", handler=lambda c, p: {},
                input_schema={"type": "object", "properties": {}, "required": []}),
        }))
    _connect()
    low = connector_tools.prompt_block().lower()
    assert "confirm" in low, connector_tools.prompt_block()


def test_the_block_is_not_added_twice(env, monkeypatch):
    """The Brain adds the block itself and so does the shared helper. If both
    paths meet, the model reads the same paragraph twice."""
    from ops_dashboard import brain

    registry.register(_spec())
    _connect()

    class _P:
        name = "openai"

        def supports_tools(self):
            return True

        def ask_with_tools(self, system, messages, tools, on_tool_call=None, **kwargs):
            assert system.count("CONNECTED APPS") == 1, system
            return "ok"

    answer, used = brain.ask_with_connector_tools(
        _P(), "base prompt", [{"role": "user", "content": "hi"}])
    assert used is True
    assert answer == "ok"


def test_the_brain_prompt_carries_the_block(env, monkeypatch):
    """The Brain's own path adds the block too."""
    from ops_dashboard import brain

    registry.register(_spec())
    _connect()

    captured: dict = {}

    class _P:
        name = "openai"

        def supports_tools(self):
            return True

        def ask_with_tools(self, system, messages, tools, on_tool_call=None, **kwargs):
            captured["system"] = system
            return "ok"

    monkeypatch.setattr(brain, "resolve_provider", lambda *a, **kw: _P())
    brain.ask("what issues are open?", use_tools=True)
    assert "CONNECTED APPS" in captured.get("system", "")
