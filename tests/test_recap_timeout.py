"""Guards the wall-clock deadline on the Holding-CEO recap synthesis.

The synthesis runs on the HTTP request thread; a slow provider (a local Ollama
model can generate for up to 120s) must not be able to block it. These tests
verify the deadline trips fast and that recap_synthesis degrades to the static
brief instead of hanging.

    .venv/bin/pytest tests/test_recap_timeout.py -v
"""
from __future__ import annotations

import time

import pytest

from ops_dashboard import boardroom


class _SlowProvider:
    name = "slow-test"

    def ask(self, system, messages):
        time.sleep(5)  # far longer than the deadline below
        return "should never be used"


def test_deadline_trips_fast():
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        boardroom._ask_with_deadline(_SlowProvider(), "sys", [{"role": "user", "content": "x"}], 0.3)
    elapsed = time.monotonic() - start
    assert elapsed < 1.5, f"deadline did not return promptly (took {elapsed:.2f}s)"


def test_fast_provider_returns_value():
    class _Fast:
        name = "fast"

        def ask(self, system, messages):
            return "quick answer"

    out = boardroom._ask_with_deadline(_Fast(), "sys", [{"role": "user", "content": "x"}], 5)
    assert out == "quick answer"


def test_recap_synthesis_falls_back_to_static_on_timeout(tmp_path, monkeypatch):
    """A slow configured provider must degrade to the static brief, fast."""
    db_path = tmp_path / "recap.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(db_path))
    from ops_dashboard import db
    db.DB_PATH = db_path  # type: ignore[attr-defined]
    db.init_db()

    # Tight deadline so the test is quick; patch the module constant.
    monkeypatch.setattr(boardroom, "_RECAP_LLM_TIMEOUT", 0.3)
    from ops_dashboard import brain
    monkeypatch.setattr(brain, "resolve_provider", lambda *a, **k: _SlowProvider())

    payload = {"ceos": [], "recent_messages": [], "projects": []}
    start = time.monotonic()
    result = boardroom.recap_synthesis(payload, force=True)
    elapsed = time.monotonic() - start

    assert result["source"] == "static", result
    assert result["text"].strip()
    assert elapsed < 3, f"recap_synthesis hung ({elapsed:.2f}s) instead of falling back"


def test_brain_ask_with_deadline_trips_fast():
    """brain.ask_with_deadline caps a slow provider the same way recap does."""
    from ops_dashboard import brain
    with pytest.raises(TimeoutError):
        brain.ask_with_deadline(_SlowProvider(), "sys", [{"role": "user", "content": "x"}], 0.3)


def test_brain_ask_with_deadline_passthrough():
    from ops_dashboard import brain

    class _Fast:
        name = "fast"

        def ask(self, system, messages):
            return "ok"

    assert brain.ask_with_deadline(_Fast(), "s", [{"role": "user", "content": "x"}], 5) == "ok"
