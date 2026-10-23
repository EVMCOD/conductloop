"""MiniMax Coding Plan: the monthly subscription is served on MiniMax's
Anthropic-compatible API, not on the pay-as-you-go balance endpoint."""
from __future__ import annotations

import contextlib
import inspect
from types import SimpleNamespace

import pytest

# The point of this test is to check the provider against the *installed*
# SDK's signature, so it cannot run without it. The CI installs it (see
# requirements-dev.txt); a lean local checkout skips it.
pytest.importorskip("anthropic")
from anthropic.resources.messages import Messages  # noqa: E402

from ops_dashboard import brain  # noqa: E402

# The fake accepts exactly what the installed SDK accepts: anthropic>=2 has no
# `temperature`, and a fake taking **kwargs hid that the provider passed it.
_ACCEPTS = {name: set(inspect.signature(getattr(Messages, name)).parameters) - {"self"}
            for name in ("create", "stream")}


def _check(method: str, kwargs: dict) -> None:
    unknown = set(kwargs) - _ACCEPTS[method]
    assert not unknown, f"Messages.{method}() does not take {sorted(unknown)}"


class FakeAnthropic:
    """Records the client arguments and returns a reasoning block before the text."""
    created: list = []

    def __init__(self, **kwargs):
        FakeAnthropic.created.append(kwargs)
        self.messages = SimpleNamespace(create=self._create, stream=self._stream)

    def _create(self, **kwargs):
        _check("create", kwargs)
        FakeAnthropic.created.append({"request": kwargs})
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="..."), SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(input_tokens=3, output_tokens=1))

    @contextlib.contextmanager
    def _stream(self, **kwargs):
        _check("stream", kwargs)
        FakeAnthropic.created.append({"stream": kwargs})
        yield SimpleNamespace(text_stream=iter(["o", "k"]))


@pytest.fixture()
def fake_anthropic(monkeypatch):
    import anthropic
    FakeAnthropic.created = []
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    return FakeAnthropic


def test_coding_plan_url_uses_the_anthropic_api(fake_anthropic):
    p = brain.MiniMaxProvider(api_key="k", base_url=brain.MINIMAX_CODING_PLAN_URL)
    assert p.model == "MiniMax-M3"
    assert p.ask("sys", [{"role": "user", "content": "hi"}]) == "ok"          # thinking block left out
    client, call = fake_anthropic.created
    assert client == {"api_key": "k", "base_url": "https://api.minimax.io/anthropic"}
    req = call["request"]
    assert req["model"] == "MiniMax-M3" and req["system"] == "sys"
    assert req["max_tokens"] == brain.CODING_PLAN_MAX_TOKENS                  # room for M3's reasoning


def test_coding_plan_streams(fake_anthropic):
    p = brain.MiniMaxProvider(api_key="k", base_url=brain.MINIMAX_CODING_PLAN_URL)
    assert "".join(p.stream("sys", [{"role": "user", "content": "hi"}])) == "ok"
    assert fake_anthropic.created[-1]["stream"]["max_tokens"] == brain.CODING_PLAN_MAX_TOKENS


def test_internal_errors_are_not_blamed_on_the_provider():
    err = brain._classify_provider_error(TypeError("create() got an unexpected keyword argument 'x'"), "minimax")
    assert err.code == "internal" and "no llegó al provider" in err.message


def test_pay_as_you_go_stays_openai_compatible(fake_anthropic):
    p = brain.MiniMaxProvider(api_key="k")
    assert p._anthropic() is None and p.base_url == "https://api.minimaxi.chat/v1"
    assert fake_anthropic.created == []


def test_anthropic_provider_without_base_url_is_unchanged(fake_anthropic):
    brain.AnthropicProvider(api_key="k").ask("sys", [{"role": "user", "content": "hi"}])
    assert fake_anthropic.created[0] == {"api_key": "k"}


def test_saving_the_plan_switches_minimax_and_shows_it(monkeypatch):
    monkeypatch.delenv("MINIMAX_BASE_URL", raising=False)
    brain.set_provider_key("minimax", base_url=brain.MINIMAX_CODING_PLAN_URL)
    assert brain.resolve_provider("minimax", strict=False).base_url == brain.MINIMAX_CODING_PLAN_URL
    row = next(r for r in brain.list_providers_status() if r["name"] == "minimax")
    assert row["plan"] == "coding"
    brain.set_provider_key("minimax", base_url="https://api.minimaxi.chat/v1")
    row = next(r for r in brain.list_providers_status() if r["name"] == "minimax")
    assert row["plan"] == "payg"
