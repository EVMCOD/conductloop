"""Talking to an agent must reach the connectors too.

The team chat is where the founder talks to an agent directly: "what issues
are open in GitHub?", "what did the CFO decide yesterday?". The agent has
its role playbook (the skills are injected into its prompt) but it had no
tools at all, so it could only answer from the vault and the conversation.

With a connector connected, it should be able to check.

The streaming matters here: the chat streams deltas while the answer is
being written. The tools add a round trip, so the text arrives in one piece
when there are tools to use, and keeps streaming when there are not. That
trade is worth it, and the rule is explicit: with connectors, one answer;
without them, the stream as before.
"""
from __future__ import annotations

import pytest

from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens
from ops_dashboard import db as dbmod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "tc.db")
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
            handler=lambda c, p: {"issues": [{"number": 7}]},
            input_schema={"type": "object",
                          "properties": {"query": {"type": "string"}},
                          "required": ["query"]})})


def _connect(cid: str = "gh"):
    store.upsert_account(cid, status="connected", permission="read",
                         granted_scopes=[], account_label="enrique",
                         client_id=None, last_error=None, connected_at=1)


class _ToolsProvider:
    name = "openai"

    def __init__(self) -> None:
        self.offered: list[dict] = []
        self.system = ""
        self.streamed = False

    def supports_tools(self) -> bool:
        return True

    def stream(self, system, messages, **kwargs):
        self.streamed = True
        yield "streamed answer"

    def ask(self, system, messages, **kwargs):
        return "plain answer"

    def ask_with_tools(self, system, messages, tools, on_tool_call=None, **kwargs):
        self.offered = list(tools)
        self.system = system
        from ops_dashboard import tools as brain_tools
        names = [t["function"]["name"] for t in tools if t.get("type") == "function"]
        target = next((n for n in names if n.startswith("connector_")), None)
        if target:
            result = brain_tools.call(target, {"query": "open"})
            if on_tool_call:
                on_tool_call(target, {"query": "open"}, result)
        return "answer from the connector"



def _ask(team_chat, provider, message: str):
    """Drive a real ChatTurn against the fake provider.

    The agent comes from the real roster, so the prompt is the one the
    product actually builds.
    """
    # The agent the roster actually offers: with no CEO of its own, the
    # built-in team is what the founder talks to.
    turn = team_chat.ChatTurn(agent_key="builtin:cfo", message=message, thread_id=None,
                              team=False, provider=None)
    turn.provider = provider
    return turn.events()

def test_the_agent_is_offered_the_connector_tools(env, monkeypatch):
    from ops_dashboard import team_chat

    registry.register(_spec())
    _connect()
    provider = _ToolsProvider()
    monkeypatch.setattr(team_chat.brain, "resolve_provider", lambda *a, **k: provider)

    list(_ask(team_chat, provider, "what issues are open?"))

    assert provider.offered, "the agent was offered no tools"
    assert any(t["function"]["name"].startswith("connector_gh_")
               for t in provider.offered), provider.offered


def test_the_agent_prompt_says_what_it_can_reach(env, monkeypatch):
    from ops_dashboard import team_chat

    registry.register(_spec())
    _connect()
    provider = _ToolsProvider()
    monkeypatch.setattr(team_chat.brain, "resolve_provider", lambda *a, **k: provider)

    list(_ask(team_chat, provider, "what issues are open?"))

    assert provider.system
    low = provider.system.lower()
    assert "connector" in low or "github" in low, provider.system[:400]


def test_without_connectors_the_answer_still_streams(env, monkeypatch):
    """The common case must not change: no connectors, no extra round trip."""
    from ops_dashboard import team_chat

    provider = _ToolsProvider()
    monkeypatch.setattr(team_chat.brain, "resolve_provider", lambda *a, **k: provider)

    out = list(_ask(team_chat, provider, "hello"))

    assert provider.streamed, "the streaming path was not used"
    assert any(e.get("type") == "delta" for e in out), out


def test_the_read_is_audited(env, monkeypatch):
    from ops_dashboard import team_chat

    registry.register(_spec())
    _connect()
    provider = _ToolsProvider()
    monkeypatch.setattr(team_chat.brain, "resolve_provider", lambda *a, **k: provider)

    list(_ask(team_chat, provider, "what issues are open?"))

    events = store.list_audit(limit=20, connector_id="gh")
    assert any(e["event"] == "read" for e in events), events
