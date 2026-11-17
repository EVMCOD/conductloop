"""A long answer is streamed, not refused.

The Anthropic SDK computes the timeout of a non-streaming request from
``max_tokens`` and refuses, before the request leaves the Mac, one whose
worst case runs for more than ten minutes. A code session may answer with
32k tokens, which is exactly such a request: it is sent streaming, where
the timeout is an hour and the answer is the same message.

The refusal is a ``ValueError`` raised by the SDK, not by the endpoint: it
is a property of the client, so the same request works everywhere.
"""
from __future__ import annotations

from ops_dashboard import brain

# The message the SDK raises.
LONG_REQUEST = ("Streaming is required for operations that may take longer "
                "than 10 minutes.")


class _Block:
    def __init__(self, type="text", text="hi"):
        self.type = type
        self.text = text


class _Resp:
    def __init__(self, text="hi"):
        self.content = [_Block(text=text)]
        self.usage = {"input_tokens": 10, "output_tokens": 5}


class _Stream:
    """What the SDK returns for a streaming request."""

    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._resp


class _Client:
    """Refuses the long non-streaming requests, like the SDK does."""

    def __init__(self, *, refuse_long=True):
        self.calls: list[dict] = []
        self.streams: list[dict] = []
        self.refuse_long = refuse_long
        self.messages = type("M", (), {"create": self._create, "stream": self._stream})()

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.refuse_long:
            raise ValueError(LONG_REQUEST)
        return _Resp()

    def _stream(self, **kwargs):
        self.streams.append(kwargs)
        return _Stream(_Resp())


def _provider(client):
    p = brain.AnthropicProvider(api_key="k", model="m")
    p._client = lambda: client          # type: ignore[method-assign]
    return p


def test_a_long_request_is_sent_streaming():
    client = _Client()
    p = _provider(client)
    answer = p.ask("you are an agent", [{"role": "user", "content": "the task"}],
                   max_tokens=32000)
    assert answer == "hi"
    assert len(client.streams) == 1
    # The same request, only streamed.
    assert client.streams[0]["max_tokens"] == 32000
    sent = client.streams[0]["messages"][0]
    assert sent["role"] == "user"
    # The text is the same, marked or not for the cache.
    content = sent["content"]
    text = content if isinstance(content, str) else content[0]["text"]
    assert text == "the task"


def test_a_short_request_is_not_streamed():
    client = _Client(refuse_long=False)
    p = _provider(client)
    answer = p.ask("you are an agent", [{"role": "user", "content": "the task"}],
                   max_tokens=1500)
    assert answer == "hi"
    assert len(client.calls) == 1
    assert client.streams == []


def test_another_value_error_is_not_hidden():
    """Only the long request is answered another way."""

    class _Strict(_Client):
        def _create(self, **kwargs):
            self.calls.append(kwargs)
            raise ValueError("max_tokens must be an integer")

    client = _Strict()
    p = _provider(client)
    try:
        p.ask("you are an agent", [{"role": "user", "content": "the task"}])
    except ValueError as e:
        assert "max_tokens" in str(e)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("the error should not have been swallowed")
    assert client.streams == []
