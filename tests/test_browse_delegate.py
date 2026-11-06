"""Delegated browse tool for team agents (gap 2).

The tool is offered to chat / triggers / Telegram. Approvals and the live tab
are the existing browser-session path; these tests stay offline: platform
refusals, argument checks, wiring into tools.py, and a mocked runner.
"""
from __future__ import annotations

import pytest

from ops_dashboard import browse_tools as bt
from ops_dashboard import platform_caps
from ops_dashboard import tools as brain_tools
from ops_dashboard.sessions.runner import RunOutcome


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    """Never open a real session in this module unless a test opts in."""
    monkeypatch.setenv("OPS_BROWSE_ALLOW_MOCK", "")


def test_tool_spec_matches_brain_shape():
    spec = bt.tool_spec()
    assert spec["name"] == "browse"
    assert "input_schema" in spec
    assert "task" in spec["input_schema"]["properties"]
    assert "task" in spec["input_schema"]["required"]
    assert "parameters" not in spec  # Anthropic shape; OpenAI wraps later


def test_browse_is_registered_in_all_tools():
    names = {t["name"] for t in brain_tools._all_tools()}
    assert "browse" in names


def test_tools_for_openai_wraps_browse():
    wrapped = brain_tools.tools_for_openai()
    names = [t["function"]["name"] for t in wrapped]
    assert "browse" in names
    browse = next(t for t in wrapped if t["function"]["name"] == "browse")
    assert browse["type"] == "function"
    assert "task" in browse["function"]["parameters"]["properties"]


def test_prompt_block_mentions_macos_and_no_screenshots(monkeypatch):
    monkeypatch.setattr(platform_caps, "browser_supported", lambda: True)
    block = bt.prompt_block()
    assert "browse" in block
    assert "screenshot" in block.lower() or "screenshots" in block.lower()
    assert "Mac" in block or "macos" in block.lower()


def test_platform_unsupported_refuses_without_session(monkeypatch):
    monkeypatch.setattr(platform_caps, "browser_supported", lambda: False)
    monkeypatch.setattr(platform_caps, "browser_message",
                        lambda: "The in-app browser is not available on Linux.")
    called = {"run": False}

    def boom(*a, **k):
        called["run"] = True
        raise AssertionError("must not open a session off macOS")

    monkeypatch.setattr(bt, "_run_delegated", boom)
    out = bt.browse({"task": "look up the docs"})
    assert out["ok"] is False
    assert out["error"] == "platform_unsupported"
    assert "Linux" in out["message"]
    assert called["run"] is False


def test_empty_task_and_bad_url_and_mode():
    assert bt.browse({"task": "  "})["error"] == "bad_request"
    assert bt.browse({"task": "x", "start_url": "ftp://no"})["error"] == "bad_request"
    assert bt.browse({"task": "x", "mode": "fly"})["error"] == "bad_request"


def test_tools_call_dispatches_to_browse(monkeypatch):
    monkeypatch.setattr(platform_caps, "browser_supported", lambda: False)
    monkeypatch.setattr(platform_caps, "browser_message", lambda: "nope")
    out = brain_tools.call("browse", {"task": "anything"})
    assert out["ok"] is False
    assert out["error"] == "platform_unsupported"


def test_run_delegated_uses_runner_summary(monkeypatch):
    monkeypatch.setattr(platform_caps, "browser_supported", lambda: True)

    class Prov:
        name = "openai"
        model = "gpt-test"

        def available(self):
            return True

    monkeypatch.setattr("ops_dashboard.brain.resolve_provider", lambda *a, **k: Prov())

    class FakeStore:
        def __init__(self):
            self.sessions = {}
            self.tasks = {}
            self._sid = 0
            self._tid = 0

        def enqueue_task(self, **kw):
            self._tid += 1
            self.tasks[self._tid] = {"id": self._tid, **kw, "result_summary": None}
            return self._tid

        def claim_task(self, sid, tid=None):
            t = self.tasks[tid]
            t["status"] = "running"
            return t

        def get_task(self, tid):
            return self.tasks.get(tid)

        def set_session_status(self, sid, status, **kw):
            self.sessions[sid] = status

        def list_tasks(self, **kw):
            return list(self.tasks.values())

    store = FakeStore()

    def fake_create(store_arg, **kw):
        store._sid += 1
        store.sessions[store._sid] = "idle"
        assert kw["browser_mode"] in ("read", "act")
        assert kw["budget_cost_cents"] == bt.DEFAULT_BUDGET_CENTS
        return store._sid

    class FakeRunner:
        def __init__(self, store_arg, sid, **kw):
            self.store = store_arg
            self.sid = sid

        def run_task(self, task):
            tid = task["id"]
            self.store.tasks[tid]["result_summary"] = "Found the price: 12 EUR."
            return RunOutcome(task_status="needs_review", session_status="idle",
                              reason="Found the price: 12 EUR.", iterations=2)

    monkeypatch.setattr("ops_dashboard.sessions.SessionStore", lambda: store)
    monkeypatch.setattr("ops_dashboard.sessions.browser.create_browser_session", fake_create)
    monkeypatch.setattr("ops_dashboard.sessions.browser_runner.BrowserRunner", FakeRunner)

    out = bt.browse({"task": "What is the price?", "start_url": "https://example.com",
                     "mode": "read"})
    assert out["ok"] is True
    assert out["summary"] == "Found the price: 12 EUR."
    assert out["session_id"] == 1
    assert out["task_id"] == 1
    assert "screenshot" not in out
    assert "dom" not in out


def test_mock_provider_refused_without_allow(monkeypatch):
    monkeypatch.setattr(platform_caps, "browser_supported", lambda: True)

    class Mock:
        name = "mock"
        model = "mock"

        def available(self):
            return True

    monkeypatch.setattr("ops_dashboard.brain.resolve_provider", lambda *a, **k: Mock())
    out = bt.browse({"task": "x"})
    assert out["ok"] is False
    assert out["error"] == "no_provider"


def test_ask_with_connector_tools_offers_browse_without_connectors(monkeypatch):
    """Team chat / Telegram Holding CEO path: browse alone is enough to use tools."""
    from ops_dashboard import brain

    offered = {}

    class Prov:
        name = "openai"

        def supports_tools(self):
            return True

        def ask(self, *a, **k):
            raise AssertionError("plain ask must not be used when browse is offered")

        def ask_with_tools(self, system, messages, tools, on_tool_call=None, **kw):
            offered["system"] = system
            offered["names"] = [
                (t.get("function") or {}).get("name") or t.get("name") for t in tools
            ]
            return "ok"

    # No connectors connected: _connector_tools is empty, browse remains.
    monkeypatch.setattr("ops_dashboard.tools._connector_tools", lambda: [])
    text, used = brain.ask_with_connector_tools(
        Prov(), "sys", [{"role": "user", "content": "hi"}])
    assert used is True
    assert text == "ok"
    assert "browse" in offered["names"]
    assert "BROWSER" in offered["system"]
    assert "browse" in offered["system"]
