"""The last two paths: the orchestrator and the Holding CEO.

The orchestrator already offers the tools but never says what they are — a
tool the prompt does not mention is a tool the model does not reach for. The
Holding CEO (the chairman's quick question over Telegram or Discord) calls
the provider with no tools at all.

Both are the same defect in two places: the connector capability exists on
one side and the conversation on the other, and nothing joins them.
"""
from __future__ import annotations

import pytest

from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens
from ops_dashboard import db as dbmod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "oc.db")
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


class _ToolsProvider:
    name = "openai"

    def __init__(self) -> None:
        self.offered: list[dict] = []
        self.system = ""

    def supports_tools(self) -> bool:
        return True

    def ask(self, system, messages, **kwargs):
        return "plain answer"

    def ask_with_tools(self, system, messages, tools, on_tool_call=None, **kwargs):
        self.offered = list(tools)
        self.system = system
        return "answer with tools"


def test_the_holding_ceo_offers_the_connector_tools(env, monkeypatch):
    """The chairman's quick question over Telegram or Discord."""
    from ops_dashboard import boardroom, brain

    registry.register(_spec())
    _connect()
    provider = _ToolsProvider()
    monkeypatch.setattr(brain, "resolve_provider", lambda *a, **k: provider)

    boardroom.chat_with_holding_ceo("what is open in GitHub?")

    assert provider.offered, "the Holding CEO was offered no tools"
    assert any(t["function"]["name"].startswith("connector_gh_")
               for t in provider.offered), provider.offered


def test_the_holding_ceo_prompt_says_what_it_can_reach(env, monkeypatch):
    from ops_dashboard import boardroom, brain

    registry.register(_spec())
    _connect()
    provider = _ToolsProvider()
    monkeypatch.setattr(brain, "resolve_provider", lambda *a, **k: provider)

    boardroom.chat_with_holding_ceo("what is open in GitHub?")

    assert provider.system
    low = provider.system.lower()
    assert "connector" in low or "github" in low, provider.system[:400]


def test_the_orchestrator_prompt_carries_the_block(env, monkeypatch):
    """The orchestrator already offers the tools; it must say what they are."""
    from ops_dashboard import connector_tools

    registry.register(_spec())
    _connect()
    block = connector_tools.prompt_block()
    assert "CONNECTED APPS" in block

    # And the orchestrator's own prompt path adds it.
    from ops_dashboard import orchestrator
    src = open(orchestrator.__file__, encoding="utf-8").read()
    assert "prompt_block" in src, "the orchestrator never mentions the connectors"


def test_a_provider_without_tools_still_answers_the_holding_ceo(env, monkeypatch):
    from ops_dashboard import boardroom, brain

    registry.register(_spec())
    _connect()

    class _NoTools:
        name = "openai"

        def supports_tools(self):
            return False

        def ask(self, system, messages, **kwargs):
            return "plain answer"

    monkeypatch.setattr(brain, "resolve_provider", lambda *a, **k: _NoTools())
    answer = boardroom.chat_with_holding_ceo("hello")
    assert "plain answer" in answer or answer
