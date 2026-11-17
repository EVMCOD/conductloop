"""The prompt cache.

The system prompt and the task of a session do not change from one turn to
the next, and the conversation is sent again and again. An endpoint that
speaks Anthropic's API can keep that prefix: the tokens of the later turns
are only the part that is new.

The markers are on the system and on the FIRST user message only: what comes
after it changes on every turn (the ledger of what the agent has already
done), and a marker there would be invalidated every time.
"""
from __future__ import annotations

import json

from ops_dashboard import brain

# ── the markers ────────────────────────────────────────────────────────────

def test_the_system_prompt_is_marked_for_the_cache():
    blocks = brain._cached_system("you are an agent")
    assert blocks == [{"type": "text", "text": "you are an agent",
                       "cache_control": {"type": "ephemeral"}}]


def test_the_first_user_message_is_marked_for_the_cache():
    out = brain._mark_cache([{"role": "user", "content": "the task"},
                             {"role": "assistant", "content": "ok"}])
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert out[0]["content"][0]["text"] == "the task"
    # The rest is not marked: it changes on every turn.
    assert out[1] == {"role": "assistant", "content": "ok"}


def test_only_the_first_user_message_is_marked():
    out = brain._mark_cache([{"role": "user", "content": "the task"},
                             {"role": "user", "content": "the ledger"}])
    assert "cache_control" in out[0]["content"][0]
    assert "cache_control" not in out[1]["content"][0]


def test_a_message_that_is_already_blocks_keeps_its_shape():
    out = brain._mark_cache([{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a", "content": "x"}]}])
    assert out[0]["content"][0]["type"] == "tool_result"
    assert "cache_control" not in out[0]["content"][0]


def test_no_user_message_leaves_the_conversation_alone():
    messages = [{"role": "assistant", "content": "ok"}]
    assert brain._mark_cache(messages) == messages


# ── the usage ──────────────────────────────────────────────────────────────

def test_the_cache_tokens_are_reported():
    usage = brain.extract_usage({"input_tokens": 100, "output_tokens": 20,
                                 "cache_read_input_tokens": 5000,
                                 "cache_creation_input_tokens": 300})
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 20
    assert usage["cache_read_input_tokens"] == 5000
    assert usage["cache_creation_input_tokens"] == 300


def test_a_provider_without_a_cache_reports_nothing_extra():
    usage = brain.extract_usage({"input_tokens": 100, "output_tokens": 20})
    assert "cache_read_input_tokens" not in usage


# ── the request ────────────────────────────────────────────────────────────

class _Block:
    def __init__(self, type="text", text="hi"):
        self.type = type
        self.text = text


class _Resp:
    def __init__(self):
        self.content = [_Block()]
        self.usage = {"input_tokens": 10, "output_tokens": 5}


class _Client:
    """Records what it was asked, and refuses the cache markers once."""

    def __init__(self, *, refuse_cache=False):
        self.calls: list[dict] = []
        self.messages = type("M", (), {"create": self._create})()
        self.refuse_cache = refuse_cache

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.refuse_cache and "cache_control" in json.dumps(kwargs, default=str):
            raise RuntimeError("cache_control is not supported by this endpoint")
        return _Resp()


def _provider(client):
    p = brain.AnthropicProvider(api_key="k", model="m")
    p._client = lambda: client          # type: ignore[method-assign]
    return p


def test_the_request_carries_the_cache_markers():
    client = _Client()
    p = _provider(client)
    p.ask("you are an agent", [{"role": "user", "content": "the task"}])
    sent = client.calls[0]
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_an_endpoint_that_does_not_know_the_cache_is_asked_again_without_them():
    client = _Client(refuse_cache=True)
    p = _provider(client)
    answer = p.ask("you are an agent", [{"role": "user", "content": "the task"}])
    assert answer == "hi"
    assert len(client.calls) == 2
    # The second request is the plain one.
    assert client.calls[1]["system"] == "you are an agent"
    assert client.calls[1]["messages"][0]["content"] == "the task"


def test_a_step_carries_the_cache_markers_too():
    client = _Client()
    p = _provider(client)
    p.ask_step("you are an agent", [{"role": "user", "content": "the task"}], [])
    sent = client.calls[0]
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
