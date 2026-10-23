"""The brain's OpenAI-compatible provider: any model of any company.

OpenAI itself is one server among many that speak the same dialect. This
file proves the provider reaches all of them — a cloud API, a local runtime
that needs no key — and that agents can use tools through it, because a
provider the agents cannot call is a provider the product cannot use.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ops_dashboard import brain


class _OpenAIHandler(BaseHTTPRequestHandler):
    """A server that speaks the OpenAI API: /models and /chat/completions."""

    requests: list[str] = []

    def log_message(self, *_a):
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).requests.append(self.path)
        if self.path.endswith("/models"):
            self._send({"data": [{"id": "local-model-1"}, {"id": "local-model-2"}]})
        else:
            self._send({"error": "not found"}, status=404)

    def do_POST(self):
        type(self).requests.append(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw or b"{}")
        if self.path.endswith("/chat/completions"):
            if body.get("tools") and not any(m.get("role") == "tool" for m in body.get("messages") or []):
                # The model asks for a tool, the way the real ones do.
                self._send({
                    "choices": [{"message": {
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": "call_1", "type": "function",
                            "function": {"name": "search", "arguments": '{"q": "x"}'},
                        }],
                    }}],
                })
            else:
                self._send({"choices": [{"message": {"role": "assistant",
                                                     "content": "hola desde el mock"}}]})
        else:
            self._send({"error": "not found"}, status=404)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def _clear_cache():
    brain._OPENAI_MODELS_CACHE.clear()
    yield
    brain._OPENAI_MODELS_CACHE.clear()


# ── reaching any server ───────────────────────────────────────────────────

def test_a_local_server_needs_no_key(server):
    """LM Studio and llama.cpp do not ask for a key; a base url is enough."""
    provider = brain.OpenAIProvider(base_url=server)
    assert provider.available() is True


def test_the_server_is_asked_which_models_it_has(server):
    provider = brain.OpenAIProvider(base_url=server)
    assert provider._server_models() == ["local-model-1", "local-model-2"]


def test_with_no_model_the_server_decides(server):
    """Pointing it at a local runtime must work without being told a model."""
    provider = brain.OpenAIProvider(base_url=server)
    assert provider.model == "local-model-1"


def test_a_model_can_still_be_pinned(server):
    provider = brain.OpenAIProvider(base_url=server, model="local-model-2")
    assert provider.model == "local-model-2"


def test_a_server_that_is_not_there_is_not_available():
    provider = brain.OpenAIProvider(base_url="http://127.0.0.1:9/v1")
    assert provider.available() is False


def test_without_a_base_url_a_key_is_what_makes_it_available():
    assert brain.OpenAIProvider(api_key="sk-x").available() is True
    assert brain.OpenAIProvider(api_key="").available() is False


def test_the_models_are_asked_once_and_remembered(server):
    """The server is asked once, not on every call."""
    _OpenAIHandler.requests = []
    provider = brain.OpenAIProvider(base_url=server)
    provider._server_models()
    provider._server_models()
    provider._server_models()
    assert len([p for p in _OpenAIHandler.requests if p.endswith("/models")]) == 1


# ── the provider is chosen, not the vendor ────────────────────────────────

def test_a_local_server_is_used_before_the_cloud(monkeypatch):
    """Local work is free and private; it goes first, like the media ones."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    order = brain._auto_order({"openai_base_url": "http://127.0.0.1:1234/v1"})
    assert order.index("openai") < order.index("anthropic")
    assert order[0] == "ollama"


def test_a_cloud_server_keeps_the_happy_order(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    order = brain._auto_order({"openai_base_url": "https://api.deepseek.com/v1"})
    assert order.index("anthropic") < order.index("openai")


def test_without_a_base_url_the_order_is_the_happy_one(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert brain._auto_order({}) == list(brain._AUTO_ORDER)


def test_the_status_shows_where_it_points(monkeypatch):
    monkeypatch.setattr(brain, "_load_config",
                        lambda: {"openai_base_url": "http://127.0.0.1:1234/v1"})
    row = next(r for r in brain.list_providers_status() if r["name"] == "openai")
    assert row["base_url"] == "http://127.0.0.1:1234/v1"
    assert row["local"] is True


# ── tools ─────────────────────────────────────────────────────────────────

def test_agents_can_use_tools_through_it(server, monkeypatch):
    """A provider the agents cannot call is a provider the product cannot use."""
    called = []
    import ops_dashboard.tools as brain_tools

    monkeypatch.setattr(brain_tools, "call", lambda name, args: {"ok": True, "tool": name})
    provider = brain.OpenAIProvider(base_url=server, model="local-model-1")

    def fake_call(name, args, result):
        called.append((name, args))

    out = provider.ask_with_tools("system", [{"role": "user", "content": "busca"}],
                                   [{"type": "function", "function": {"name": "search"}}],
                                   fake_call)
    assert called == [("search", {"q": "x"})]
    # The tool's answer goes back to the model, and it finishes the turn.
    assert out == "hola desde el mock"


def test_it_says_the_provider_supports_tools(server):
    assert brain.OpenAIProvider(base_url=server).supports_tools() is True
