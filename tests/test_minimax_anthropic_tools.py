"""MiniMax on its Anthropic-compatible endpoint must get Anthropic-shaped tools.

ask_with_connector_tools picks the tool shape by provider.name, so MiniMax
pointed at https://api.minimax.io/anthropic (the Coding Plan) got the OpenAI
shape and the Messages API answered 400 "invalid params, function name is
empty (2013)": team mode's final answer and the Brain failed whenever tools
were offered (the browser tool on a Mac, any connector).
"""
from types import SimpleNamespace

from ops_dashboard import brain
from ops_dashboard import tools as brain_tools


class _FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="the answer")])


def _provider(monkeypatch):
    fake = _FakeMessages()
    monkeypatch.setattr(brain.AnthropicProvider, "_client",
                        lambda self: SimpleNamespace(messages=fake))
    p = brain.MiniMaxProvider(api_key="k", base_url="https://api.minimax.io/anthropic")
    return p, fake


def test_openai_shaped_tools_reach_the_anthropic_endpoint_with_a_name(monkeypatch):
    p, fake = _provider(monkeypatch)
    out = p.ask_with_tools("sys", [{"role": "user", "content": "hi"}], brain_tools.tools_for_openai())
    assert out == "the answer"
    sent = fake.calls[0]["tools"]
    assert sent and all(t.get("name") and "input_schema" in t and "function" not in t for t in sent)
    assert [t["name"] for t in sent] == [t["name"] for t in brain_tools.tools_for_anthropic()]


def test_anthropic_shaped_tools_pass_through(monkeypatch):
    p, fake = _provider(monkeypatch)
    tools = brain_tools.tools_for_anthropic()
    p.ask_with_tools("sys", [{"role": "user", "content": "hi"}], tools)
    assert fake.calls[0]["tools"] == tools


def test_the_team_mode_path_sends_named_tools(monkeypatch):
    """ask_with_connector_tools (team mode's answer) with MiniMax in Anthropic mode."""
    from ops_dashboard import platform_caps
    p, fake = _provider(monkeypatch)
    monkeypatch.setattr(platform_caps, "browser_supported", lambda: True)
    names = [t["name"] for t in brain_tools.tools_for_anthropic()]
    if "browse" not in names:   # the check needs a tool the path offers
        return
    answer, _used = brain.ask_with_connector_tools(p, "sys", [{"role": "user", "content": "hi"}])
    assert answer == "the answer"
    assert fake.calls, "the tools path was not taken"
    assert all(t.get("name") for t in fake.calls[0]["tools"])
