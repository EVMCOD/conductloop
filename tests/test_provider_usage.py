"""Real token usage exposed by brain providers (``last_usage``) from recorded
response shapes — fake SDK modules / HTTP, no network, no keys — and the
session runner preferring it over the ~4 chars/token estimate."""
from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace as NS

import pytest

from ops_dashboard import brain
from ops_dashboard.sessions.runner import usage_tokens

# Recorded shapes (trimmed) from each provider's public docs.
ANTHROPIC_MSG = NS(content=[NS(type="text", text="hi there")],
                   usage=NS(input_tokens=12, output_tokens=34, cache_read_input_tokens=0))
OPENAI_RESP = NS(choices=[NS(message=NS(content="hello"))],
                 usage=NS(prompt_tokens=20, completion_tokens=5, total_tokens=25))
# platform.minimax.io text-post example: reasoning counted inside completion.
MINIMAX_RESP = {"choices": [{"message": {"content": "您好！请问有什么可以帮您？"}}],
                "usage": {"total_tokens": 249, "total_characters": 0, "prompt_tokens": 26,
                          "completion_tokens": 223,
                          "completion_tokens_details": {"reasoning_tokens": 214}}}
# Ollama /api/chat non-streaming response.
OLLAMA_JSON = {"model": "llama3.2", "message": {"role": "assistant", "content": "yo"},
               "done": True, "total_duration": 5_191_566_416, "prompt_eval_count": 26,
               "eval_count": 298}


class _Chat:
    def __init__(self, resp):
        self.completions = NS(create=lambda **kw: resp)


def _fake_openai(monkeypatch, resp):
    mod = types.ModuleType("openai")
    if isinstance(resp, dict):  # MiniMax recorded JSON → attribute access like the SDK
        resp = json.loads(json.dumps(resp), object_hook=lambda d: NS(**d))
    mod.OpenAI = lambda **kw: NS(chat=_Chat(resp))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", mod)


def test_anthropic_usage(monkeypatch):
    mod = types.ModuleType("anthropic")
    mod.Anthropic = lambda **kw: NS(messages=NS(create=lambda **k: ANTHROPIC_MSG))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    p = brain.AnthropicProvider(api_key="fake", model="m")
    assert p.ask("s", [{"role": "user", "content": "x"}]) == "hi there"
    assert p.last_usage == {"input_tokens": 12, "output_tokens": 34, "source": "input_output"}


def test_openai_usage(monkeypatch):
    _fake_openai(monkeypatch, OPENAI_RESP)
    p = brain.OpenAIProvider(api_key="fake", model="m")
    assert p.ask("s", [{"role": "user", "content": "x"}]) == "hello"
    assert p.last_usage == {"input_tokens": 20, "output_tokens": 5, "source": "prompt_completion"}


def test_minimax_usage_from_documented_response(monkeypatch):
    _fake_openai(monkeypatch, MINIMAX_RESP)
    p = brain.MiniMaxProvider(api_key="fake", model="MiniMax-M2.7")
    assert p.ask("s", [{"role": "user", "content": "你好"}]).startswith("您好")
    assert p.last_usage == {"input_tokens": 26, "output_tokens": 223,
                            "source": "prompt_completion"}


def test_gateway_usage_and_metering_still_work(monkeypatch):
    _fake_openai(monkeypatch, OPENAI_RESP)
    metered = []
    from ops_dashboard import db
    monkeypatch.setattr(db, "log_usage", lambda **kw: metered.append(kw), raising=False)
    p = brain.GatewayProvider(api_key="fake", model="openai/gpt-x")
    p.ask("s", [{"role": "user", "content": "x"}])
    assert p.last_usage["input_tokens"] == 20 and p.last_usage["output_tokens"] == 5
    assert metered and metered[0]["prompt_tokens"] == 20


def test_ollama_usage(monkeypatch):
    import httpx

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return OLLAMA_JSON

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    p = brain.OllamaProvider(model="llama3.2")
    assert p.ask("s", [{"role": "user", "content": "x"}]) == "yo"
    assert p.last_usage == {"input_tokens": 26, "output_tokens": 298,
                            "source": "ollama_eval_count"}


def test_missing_usage_is_none_and_resets_between_calls(monkeypatch):
    _fake_openai(monkeypatch, OPENAI_RESP)
    p = brain.OpenAIProvider(api_key="fake", model="m")
    p.ask("s", [])
    assert p.last_usage
    _fake_openai(monkeypatch, NS(choices=[NS(message=NS(content="x"))], usage=None))
    p.ask("s", [])
    assert p.last_usage is None
    assert brain.MockProvider().last_usage is None


def test_extract_usage_shapes():
    assert brain.extract_usage(None) is None
    assert brain.extract_usage({}) is None
    assert brain.extract_usage({"total_tokens": 9}) == {
        "input_tokens": 0, "output_tokens": 9, "source": "total_only", "split_estimated": True}
    assert brain.extract_usage({"prompt_tokens": "7", "completion_tokens": None}) == {
        "input_tokens": 7, "output_tokens": 0, "source": "prompt_completion"}


@pytest.mark.parametrize("usage,expected", [
    ({"input_tokens": 0, "output_tokens": 40, "source": "input_output"}, (0, 40, False, "input_output")),
    (None, (100, 3, True, "estimate")),                       # 12 chars → 3 tokens
    ({"input_tokens": 0, "output_tokens": 9, "source": "total_only", "split_estimated": True},
     (0, 9, True, "total_only")),
])
def test_runner_prefers_real_usage(usage, expected):
    assert usage_tokens(usage, 100, "x" * 12) == expected
