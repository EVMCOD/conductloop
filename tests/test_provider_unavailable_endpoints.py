"""Every brain-calling endpoint returns the same stable JSON when an explicitly
requested provider is cooling down (HTTP 503 ``provider_unavailable``).

Boots the real ``DashboardHandler`` on an ephemeral loopback port against a
throwaway DB. No network, no real keys: the provider is put in cooldown with
``brain._mark_provider_failure`` and refused before any HTTP call is made.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import brain, provider_errors

FAKE_KEY = "sk-test-not-a-real-key"
EXPECTED_KEYS = {"ok", "error", "code", "provider", "model", "retry_after", "reason", "message"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for var in ("BRAIN_PROVIDER", "BRAIN_MODEL", "OLLAMA_MODEL", "OPENAI_API_KEY",
                "MINIMAX_API_KEY", "GATEWAY_API_KEY", "CONDUCTLOOP_DEMO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "pu.db"))
    monkeypatch.setattr(brain, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(brain, "_PROVIDER_LAST_FAILURE", {})
    monkeypatch.setattr(brain, "list_providers_status", lambda: [
        {"name": "anthropic", "available": True, "model": None},
        {"name": "mock", "available": True, "model": None},
    ])

    def _no_network(*_a, **_k):  # a refused provider must never be called
        raise AssertionError("provider was called despite cooldown")

    monkeypatch.setattr(brain.AnthropicProvider, "ask", _no_network)
    from ops_dashboard import db as _db
    _db.DB_PATH = tmp_path / "pu.db"  # type: ignore[attr-defined]
    _db.init_db()
    brain._mark_provider_failure("anthropic")

    from ops_dashboard.server import DashboardHandler

    class _Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", _db
    finally:
        server.shutdown()


def _req(url, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _assert_payload(status, headers, body, *, model=None):
    assert status == 503, body
    assert headers.get("Content-Type", "").startswith("application/json")
    d = json.loads(body)
    assert EXPECTED_KEYS <= set(d), d
    assert d["error"] == "provider_unavailable" and d["code"] == "provider_unavailable"
    assert d["ok"] is False
    assert d["provider"] == "anthropic"
    assert d["model"] == model
    assert d["reason"] == "cooldown"
    assert isinstance(d["retry_after"], int) and 0 < d["retry_after"] <= 600
    assert headers.get("Retry-After") == str(d["retry_after"])
    assert FAKE_KEY not in body.decode() and "sk-" not in body.decode()
    assert "Traceback" not in body.decode()
    return d


def _member(_db, model):
    tid = _db.create_team("t-pu", "T")
    return _db.add_member(tid, "Ada", type="agent", role="dev", model=model)


def test_brain_ask(env):
    base, _ = env
    _assert_payload(*_req(base + "/api/brain/ask", {"question": "hi", "provider": "anthropic"}))


def test_brain_ask_tools(env):
    base, _ = env
    _assert_payload(*_req(base + "/api/brain/ask-tools", {"question": "hi", "provider": "anthropic"}))


def test_brain_stream_refuses_before_sse(env):
    base, _ = env
    _assert_payload(*_req(base + "/api/brain/stream?question=hi&provider=anthropic"))


@pytest.mark.parametrize("stream", ["0", "1"])
def test_member_chat_from_agent_model(env, stream):
    base, _db = env
    mid = _member(_db, "anthropic:claude-x")
    _assert_payload(*_req(f"{base}/api/members/{mid}/chat?stream={stream}", {"message": "hola"}))


def test_member_chat_honours_provider_selector(env):
    base, _db = env
    mid = _member(_db, None)
    _assert_payload(*_req(f"{base}/api/members/{mid}/chat?stream=1&provider=anthropic",
                          {"message": "hola"}))


def test_note_ai_action(env):
    base, _db = env
    doc = _db.create_document(source="note", path="n.md", title="N", content="some text")
    d = _assert_payload(*_req(f"{base}/api/notes/{doc}/ai-action",
                              {"action": "summarize", "provider": "anthropic"}))
    assert d["doc_id"] == doc and d["action"] == "summarize"


def test_daily_summary(env):
    base, _ = env
    _assert_payload(*_req(base + "/api/digest/ai-summary?date=2026-10-20&provider=anthropic"))


def test_agents_run_now(env):
    base, _db = env
    mid = _member(_db, "anthropic:claude-x")
    _assert_payload(*_req(base + "/api/agents/run", {"id": mid}))
    # Trigger/scheduler wakes have no request to answer: the agent log line is
    # what the Agents → Logs tab shows, so it must be parseable too.
    rows = _db.list_agent_logs(mid)
    assert any(provider_errors.parse_run_error(r.get("summary")) == "anthropic" for r in rows)


def test_agent_wake_now(env):
    base, _db = env
    mid = _member(_db, "anthropic:claude-x")
    _assert_payload(*_req(f"{base}/api/agents/{mid}/wake", {"prompt": "go"}))


def test_task_run_sync(env):
    base, _db = env
    tid = _db.create_task(None, "t", "do it", provider="anthropic", model="claude-x")
    d = _assert_payload(*_req(f"{base}/api/tasks/{tid}/run", {"sync": True}), model="claude-x")
    run = _db.get_run(d["run_id"])
    assert provider_errors.parse_run_error(run["error"]) == "anthropic"


def test_task_stream_done_event_carries_payload(env):
    base, _db = env
    tid = _db.create_task(None, "t", "do it", provider="anthropic", model="claude-x")
    status, _h, body = _req(f"{base}/api/tasks/{tid}/stream")
    assert status == 200
    events = [json.loads(line[6:]) for line in body.decode().split("\n\n")
              if line.startswith("data: ")]
    done = [e for e in events if e.get("type") == "done"][-1]
    assert done["status"] == "error"
    assert done["provider_unavailable"]["error"] == "provider_unavailable"
    assert done["provider_unavailable"]["provider"] == "anthropic"
    assert done["provider_unavailable"]["model"] == "claude-x"


def test_payload_helpers_roundtrip():
    e = brain.ProviderUnavailableError("openai", "cooldown", "msg", model="gpt-x", retry_after=42)
    assert set(e.payload()) == EXPECTED_KEYS
    assert provider_errors.parse_run_error(provider_errors.run_error_text(e)) == "openai"
    assert provider_errors.parse_run_error("RuntimeError: boom") is None
    frame = provider_errors.sse_event(e)
    assert frame.startswith(b"data: ") and json.loads(frame[6:])["type"] == "error"
    assert provider_errors.http_status_for(e.payload(), 200) == 503
    assert provider_errors.http_status_for({"ok": True}, 200) == 200
