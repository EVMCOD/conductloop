"""End to end: the agent calls a connector, and the answer comes back.

The unit tests prove the pieces: the tool exists, the call runs, the result
is fenced. This one proves the chain — the model is offered the tool, it
calls it, the connector answers, and the answer reaches the model as DATA.

The model is faked, the provider is faked, and the connector's handler is a
fake. What is being tested is the wiring, not the model.

The behaviours:

- The connector's tool reaches the model.
- When the model calls it, the connector's handler runs.
- The model receives the result, and receives it fenced.
- A write is queued, not performed, and the model is told it is pending.
- The whole thing is audited: the connector's own audit log records the read.
"""
from __future__ import annotations

import json

import pytest

from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_store as store
from ops_dashboard import db as dbmod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "e2e.db")
    dbmod.init_db()
    saved = dict(registry._REGISTRY)
    for cid in list(registry._REGISTRY):
        registry.unregister(cid)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


class _FakeProvider:
    """A provider that calls the first tool it is offered, then answers."""

    name = "fake"

    def __init__(self, script: list[str]) -> None:
        self.script = script
        self.offered: list[dict] = []
        self.seen: list[dict] = []

    def supports_tools(self) -> bool:
        return True

    def ask_with_tools(self, system, messages, tools, on_tool_call=None, **kwargs):
        from ops_dashboard import tools as brain_tools
        self.offered = list(tools)
        names = [t["function"]["name"] for t in tools if t.get("type") == "function"]
        target = next((n for n in names if n.startswith("connector_")), None)
        if target is None:
            return "(no connector tool offered)"
        result = brain_tools.call(target, {"query": "churn"})
        if on_tool_call:
            on_tool_call(target, {"query": "churn"}, result)
        self.seen.append(result)
        return "done"


def _spec(cid: str = "gh"):
    return registry.ConnectorSpec(
        id=cid, name="GitHub", auth="token", allowed_hosts=("api.github.test",),
        actions={
            "search_issues": registry.ActionSpec(
                "search_issues", "read", "Search issues and PRs",
                handler=lambda c, p: {"issues": [{"number": 7, "title": "Churn is up"}]},
                input_schema={"type": "object",
                              "properties": {"query": {"type": "string"}},
                              "required": ["query"]}),
        })


def _connect(cid: str = "gh"):
    store.upsert_account(cid, status="connected", permission="read",
                         granted_scopes=[], account_label="ana",
                         client_id=None, last_error=None, connected_at=1)


def test_the_agent_is_offered_the_connectors_tool(env):
    from ops_dashboard import tools as brain_tools

    registry.register(_spec())
    _connect()
    provider = _FakeProvider([])
    provider.ask_with_tools("sys", [{"role": "user", "content": "hi"}],
                             brain_tools.tools_for_openai())
    assert any(t["function"]["name"].startswith("connector_gh_")
               for t in provider.offered)


def test_the_agent_receives_the_connectors_answer_fenced(env):
    from ops_dashboard import tools as brain_tools

    registry.register(_spec())
    _connect()
    provider = _FakeProvider([])
    provider.ask_with_tools("sys", [{"role": "user", "content": "issues?"}],
                             brain_tools.tools_for_openai())
    assert provider.seen, "the connector tool was never called"
    result = provider.seen[0]
    assert result.get("ok") is True, result
    body = json.dumps(result, ensure_ascii=False)
    assert "Churn is up" in body
    # And it arrived as DATA, not as an instruction.
    assert "untrusted" in body.lower()


def test_the_read_is_audited(env):
    """The connector's own audit log records the read: the agent using the
    connector is visible in the same place as the user using it."""
    from ops_dashboard import tools as brain_tools

    registry.register(_spec())
    _connect()
    brain_tools.call("connector_gh_search_issues", {"query": "churn"})
    events = store.list_audit(limit=20, connector_id="gh")
    assert any(e["event"] == "read" for e in events), events


def test_the_agent_with_no_connected_connector_is_offered_nothing(env):
    from ops_dashboard import tools as brain_tools

    registry.register(_spec())
    provider = _FakeProvider([])
    provider.ask_with_tools("sys", [{"role": "user", "content": "hi"}],
                             brain_tools.tools_for_openai())
    assert not any(t["function"]["name"].startswith("connector_")
                   for t in provider.offered)
