"""Explicit per-call provider/model resolution (multi-agent PR 1).

No network, no real keys: providers are constructed with fake keys and their
``ask`` is monkeypatched; ``list_providers_status`` is stubbed so Ollama
discovery never touches localhost.
"""
from __future__ import annotations

import threading
import time

import pytest

from ops_dashboard import brain

FAKE_KEY = "sk-test-not-a-real-key"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for var in ("BRAIN_PROVIDER", "BRAIN_MODEL", "OLLAMA_MODEL", "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY", "MINIMAX_API_KEY", "GATEWAY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(brain, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(brain, "_PROVIDER_LAST_FAILURE", {})
    brain._PROVIDER_EVENTS.clear()
    # Deterministic availability: only mock is "up" unless a test says otherwise.
    monkeypatch.setattr(brain, "list_providers_status", lambda: [
        {"name": "ollama", "available": False, "model": None},
        {"name": "anthropic", "available": False, "model": None},
        {"name": "openai", "available": False, "model": None},
        {"name": "gateway", "available": False, "model": None},
        {"name": "minimax", "available": False, "model": None},
        {"name": "mock", "available": True, "model": None},
    ])
    yield
    brain._PROVIDER_EVENTS.clear()


# ── per-call model ───────────────────────────────────────────────────────────

def test_per_call_model_wins_over_global_brain_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("BRAIN_MODEL", "global-model")
    assert brain.resolve_provider("openai", model="session-model").model == "session-model"
    # Default behaviour unchanged: no per-call model -> global BRAIN_MODEL.
    assert brain.resolve_provider("openai").model == "global-model"


def test_mock_provider_accepts_model_and_defaults_to_none():
    assert brain.resolve_provider("mock", model="mock-a").model == "mock-a"
    assert brain.resolve_provider("mock").model is None  # historic behaviour


def test_two_sessions_same_provider_different_models_in_parallel(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("BRAIN_MODEL", "global-model")

    def fake_ask(self, system, messages, **kwargs):
        time.sleep(0.05)  # overlap the two calls
        return f"{self.name}:{self.model}"

    monkeypatch.setattr(brain.OpenAIProvider, "ask", fake_ask)
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def session(label: str, model: str) -> None:
        barrier.wait()
        prov = brain.resolve_provider("openai", model=model)
        results[label] = prov.ask("sys", [{"role": "user", "content": "hi"}])

    threads = [threading.Thread(target=session, args=("backend", "model-backend")),
               threading.Thread(target=session, args=("frontend", "model-frontend"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == {"backend": "openai:model-backend", "frontend": "openai:model-frontend"}


# ── no silent fallback ───────────────────────────────────────────────────────

def test_explicit_provider_in_cooldown_raises_and_never_switches(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    brain._mark_provider_failure("anthropic")
    with pytest.raises(brain.ProviderUnavailableError) as exc:
        brain.resolve_provider("anthropic", model="claude-x")
    err = exc.value
    assert isinstance(err, brain.ProviderError)
    assert (err.code, err.provider, err.reason, err.status) == (
        "provider_unavailable", "anthropic", "cooldown", 503)
    assert "won't switch" in err.message
    events = brain.recent_provider_events()
    assert events[0]["kind"] == "refused" and events[0]["provider"] == "anthropic"
    assert not any(e["kind"] == "fallback" for e in events)


def test_explicit_strict_false_keeps_legacy_fallback_but_records_it():
    brain._mark_provider_failure("openai")
    prov = brain.resolve_provider("openai", strict=False)
    assert prov.name == "mock"
    assert prov.fallback_from == "openai"
    ev = brain.recent_provider_events()[0]
    assert ev == {**ev, "kind": "fallback", "requested": "openai", "chosen": "mock",
                  "reason": "cooldown"}


def test_configured_default_in_cooldown_falls_back_visibly(monkeypatch, caplog):
    # No provider passed by the caller → configured default; historic behaviour
    # (fall back to auto) is kept for the single-agent app, but it's recorded.
    monkeypatch.setenv("BRAIN_PROVIDER", "openai")
    brain._mark_provider_failure("openai")
    with caplog.at_level("WARNING", logger="ops_dashboard.brain"):
        prov = brain.resolve_provider()
    assert prov.name == "mock" and prov.fallback_from == "openai"
    assert brain.recent_provider_events()[0]["kind"] == "fallback"
    assert any("provider event fallback" in r.getMessage() for r in caplog.records)


def test_auto_without_cooldown_records_nothing():
    prov = brain.resolve_provider()
    assert prov.name == "mock"
    assert not hasattr(prov, "fallback_from")
    assert brain.recent_provider_events() == []


def test_auto_explicit_name_is_not_strict():
    brain._mark_provider_failure("ollama")
    assert brain.resolve_provider("auto").name == "mock"


def test_recent_events_bounded_and_newest_first():
    for i in range(130):
        brain._record_provider_event("fallback", requested=f"p{i}", chosen="mock")
    events = brain.recent_provider_events(limit=500)
    assert len(events) == 100
    assert events[0]["requested"] == "p129"
    assert brain.recent_provider_events(limit=2)[1]["requested"] == "p128"


# ── orchestrator.run_task honours tasks.provider / tasks.model ───────────────

@pytest.fixture()
def orch(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "o.db"))
    monkeypatch.setenv("OPS_VAULT_ROOT", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "o.db"  # type: ignore[attr-defined]
    _db.init_db()
    from ops_dashboard import orchestrator

    calls: list[dict] = []
    real = brain.resolve_provider

    def spy(name=None, *, model=None, strict=None):
        calls.append({"name": name, "model": model, "strict": strict})
        return real(name, model=model, strict=strict)

    monkeypatch.setattr(brain, "resolve_provider", spy)
    monkeypatch.setattr(brain.MockProvider, "ask",
                        lambda self, system, messages, **kw: f"answered-by model={self.model}")
    return orchestrator, _db, calls


def test_run_task_uses_task_model(orch):
    orchestrator, _db, calls = orch
    tid = _db.create_task(None, "t", "do it", provider="mock", model="mock-special")
    res = orchestrator.run_task(tid, sync=True)
    run = _db.get_run(res["run_id"])
    assert run["status"] == "done"
    assert "model=mock-special" in run["output_text"]
    assert calls == [{"name": "mock", "model": "mock-special", "strict": True}]


def test_run_task_accepts_provider_colon_model_form(orch):
    orchestrator, _db, calls = orch
    tid = _db.create_task(None, "t", "do it", model="mock:mock-colon")
    res = orchestrator.run_task(tid, sync=True)
    assert "model=mock-colon" in _db.get_run(res["run_id"])["output_text"]
    assert calls[-1] == {"name": "mock", "model": "mock-colon", "strict": True}


def test_run_task_without_model_keeps_default_resolution(orch):
    orchestrator, _db, calls = orch
    tid = _db.create_task(None, "t", "do it")  # no provider, no model
    res = orchestrator.run_task(tid, sync=True)
    assert _db.get_run(res["run_id"])["status"] == "done"
    # Auto-picked provider, no forced model, historic non-strict resolution.
    assert calls == [{"name": "mock", "model": None, "strict": False}]


def test_rerun_of_a_default_task_keeps_default_resolution(orch):
    # The first run used to write the auto-picked provider/model into the task,
    # so the rerun pinned them as if the user had chosen them (Codex on #15).
    orchestrator, _db, calls = orch
    tid = _db.create_task(None, "t", "do it")
    orchestrator.run_task(tid, sync=True)
    task = _db.get_task(tid)
    assert task["provider"] is None and task["model"] is None
    orchestrator.run_task(tid, sync=True)
    assert calls[-1] == {"name": "mock", "model": None, "strict": False}


def test_prefixed_model_next_to_an_explicit_provider(orch):
    orchestrator, _db, calls = orch
    tid = _db.create_task(None, "t", "do it", provider="mock", model="mock:mock-colon")
    res = orchestrator.run_task(tid, sync=True)
    assert "model=mock-colon" in _db.get_run(res["run_id"])["output_text"]
    tid2 = _db.create_task(None, "t", "do it", provider="openai", model="mock:mock-colon")
    res2 = orchestrator.run_task(tid2, sync=True)
    assert res2["ok"] is False and "names provider" in res2["error"]


def test_run_task_explicit_provider_failure_errors_without_fallback(orch, monkeypatch):
    orchestrator, _db, calls = orch
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    brain._mark_provider_failure("anthropic")
    tid = _db.create_task(None, "t", "do it", provider="anthropic", model="claude-x")
    res = orchestrator.run_task(tid, sync=True)
    run = _db.get_run(res["run_id"])
    assert run["status"] == "error"
    assert run["error"].startswith("provider_unavailable:anthropic")
    assert res["error"] == "provider_unavailable" and res["provider"] == "anthropic"
    assert res["model"] == "claude-x"
    assert [c["name"] for c in calls] == ["anthropic"]  # never tried another provider


# ── visibility endpoint (route registry, not server.py) ──────────────────────

def test_provider_events_route_registered_and_serves_events():
    import json as _json

    from ops_dashboard import routes_meta  # noqa: F401  registers routes
    from ops_dashboard.routing import router

    assert ("GET", "/api/brain/provider-events") in router.registered()
    brain._record_provider_event("fallback", requested="openai", chosen="mock", reason="cooldown")

    class _H:
        path = "/api/brain/provider-events?limit=5"
        sent: tuple = ()

        def _send(self, code, body, ctype):
            self.sent = (code, _json.loads(body), ctype)

    h = _H()
    assert router.dispatch(h, "GET", "/api/brain/provider-events")
    code, body, ctype = h.sent
    assert code == 200 and ctype == "application/json"
    assert body["events"][0]["requested"] == "openai"
