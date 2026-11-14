"""The Boardroom's agents must be able to use the connectors.

The connector tools work — the Brain and the orchestrator offer them and the
model can call them. But the Boardroom is where the agents live: the twelve
CEOs, each with its own role playbook, each answering the chairman. And that
path calls ``provider.ask`` directly, with no tools at all.

So the feature that was built to make the agents useful lands on the one
surface where the agents do not use it. The CFO with a pricing playbook
cannot look at an issue in GitHub, because the CEO reply never offers a tool.

This closes that:

- The CEO's reply path uses the tools when the provider supports them.
- The system prompt tells the model what it has and when to use it. A tool
  the prompt never mentions is a tool the model does not reach for.
- The prompt keeps its rules: no invented figures, no invented URLs. A tool
  that returns real data is the way to answer, not a licence to skip the
  rules.
- A read that happens is recorded, so the audit trail shows the agent used
  the connector.
- When the provider cannot do tools, the reply still works: the old path.

The same framing is added to the team chat, where an agent answers the
founder directly.
"""
from __future__ import annotations

import pytest

from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens
from ops_dashboard import db as dbmod


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A database with a CEO in it, and an empty registry."""
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "br.db")
    dbmod.init_db()
    tokens.use_backend(tokens.MemoryBackend())
    saved = dict(registry._REGISTRY)
    for cid in list(registry._REGISTRY):
        registry.unregister(cid)
    with dbmod.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO teams (key, name, type, created_at, updated_at) "
            "VALUES ('corp', 'Corp', 'mixed', 0, 0)")
        conn.execute(
            "INSERT INTO team_members (team_id, name, type, role, status, capabilities, "
            "notes, level, is_ceo, project_slug, created_at, updated_at) "
            "VALUES (1, 'Atlas', 'ai_agent', 'cfo', 'active', '{}', 'CFO', 'ceo', 1, "
            "'acme', 0, 0)")
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


def _spec(cid: str = "gh"):
    return registry.ConnectorSpec(
        id=cid, name="GitHub", auth="token", allowed_hosts=("api.github.test",),
        actions={"search_issues": registry.ActionSpec(
            "search_issues", "read", "Search issues and PRs",
            handler=lambda c, p: {"issues": [{"number": 7, "title": "Churn up"}]},
            input_schema={"type": "object",
                          "properties": {"query": {"type": "string"}},
                          "required": ["query"]})})


def _connect(cid: str = "gh"):
    store.upsert_account(cid, status="connected", permission="read",
                         granted_scopes=[], account_label="ana",
                         client_id=None, last_error=None, connected_at=1)


class _ToolsProvider:
    """A provider that supports tools and records what it was offered."""

    name = "openai"

    def __init__(self) -> None:
        self.offered: list[dict] = []
        self.system = ""
        self.asked = False

    def supports_tools(self) -> bool:
        return True

    def ask(self, system, messages, **kwargs):
        self.asked = True
        return "plain answer"

    def ask_with_tools(self, system, messages, tools, on_tool_call=None, **kwargs):
        self.offered = list(tools)
        self.system = system
        from ops_dashboard import tools as brain_tools
        names = [t["function"]["name"] for t in tools if t.get("type") == "function"]
        target = next((n for n in names if n.startswith("connector_")), None)
        if target:
            result = brain_tools.call(target, {"query": "churn"})
            if on_tool_call:
                on_tool_call(target, {"query": "churn"}, result)
        return "answer using the connector"


def test_the_ceo_reply_path_offers_the_connector_tools(env, monkeypatch):
    """The gap this closes: the CEO reply had no tools at all."""
    from ops_dashboard import boardroom, brain

    registry.register(_spec())
    _connect()
    provider = _ToolsProvider()
    monkeypatch.setattr(brain, "resolve_provider", lambda **kw: provider)

    boardroom.generate_ceo_reply(1, None)

    assert provider.offered, "the CEO reply was offered no tools"
    assert any(t["function"]["name"].startswith("connector_gh_")
               for t in provider.offered), provider.offered


def test_the_ceo_prompt_says_what_the_agent_has(env, monkeypatch):
    """A tool the prompt never mentions is a tool the model does not reach
    for."""
    from ops_dashboard import boardroom, brain

    registry.register(_spec())
    _connect()
    provider = _ToolsProvider()
    monkeypatch.setattr(brain, "resolve_provider", lambda **kw: provider)

    boardroom.generate_ceo_reply(1, None)

    assert provider.system, "the prompt was empty"
    low = provider.system.lower()
    assert "connector" in low or "github" in low, provider.system[:400]


def test_the_prompt_keeps_the_no_invented_figures_rule(env, monkeypatch):
    """The tool does not license skipping the rules."""
    from ops_dashboard import boardroom, brain

    registry.register(_spec())
    _connect()
    provider = _ToolsProvider()
    monkeypatch.setattr(brain, "resolve_provider", lambda **kw: provider)

    boardroom.generate_ceo_reply(1, None)

    assert "NEVER invent" in provider.system, provider.system[:400]


def test_the_prompt_says_the_results_are_data_not_instructions(env, monkeypatch):
    """Everything a connector returns is attacker-controlled."""
    from ops_dashboard import boardroom, brain

    registry.register(_spec())
    _connect()
    provider = _ToolsProvider()
    monkeypatch.setattr(brain, "resolve_provider", lambda **kw: provider)

    boardroom.generate_ceo_reply(1, None)

    low = provider.system.lower()
    assert "data" in low and "instruction" in low, provider.system[:600]


def test_a_provider_without_tools_still_answers(env, monkeypatch):
    """The old path must keep working."""
    from ops_dashboard import boardroom, brain

    registry.register(_spec())
    _connect()

    class _NoTools:
        name = "openai"

        def supports_tools(self):
            return False

        def ask(self, system, messages, **kwargs):
            return "plain answer"

    monkeypatch.setattr(brain, "resolve_provider", lambda **kw: _NoTools())
    boardroom.generate_ceo_reply(1, None)


def test_the_read_is_audited(env, monkeypatch):
    """The connector's own audit log records the agent's read."""
    from ops_dashboard import boardroom, brain

    registry.register(_spec())
    _connect()
    provider = _ToolsProvider()
    monkeypatch.setattr(brain, "resolve_provider", lambda **kw: provider)

    boardroom.generate_ceo_reply(1, None)

    events = store.list_audit(limit=20, connector_id="gh")
    assert any(e["event"] == "read" for e in events), events
