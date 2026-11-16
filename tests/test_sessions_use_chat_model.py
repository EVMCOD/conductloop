"""A new session uses what the chat already uses: its provider, its model and
its key. Only the repository and the base branch have to be given.

Starting a code session asked for a role, a provider, a model, a keychain
reference, a budget and, in another panel, a price per million tokens, all of
it already set up once in Credentials. With the MiniMax Coding Plan (a
monthly subscription) the price question had no answer at all, and without
one the session stopped before it started.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_sessions_api import J, Mock, _task, _until, api  # noqa: F401  (the HTTP fixture)

from ops_dashboard import brain
from ops_dashboard.sessions import providers

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def chat(monkeypatch, tmp_path):
    """The chat's configuration, as Credentials leaves it."""
    cfg: dict = {}
    monkeypatch.setattr(brain, "_load_config", lambda: dict(cfg))
    monkeypatch.delenv("BRAIN_PROVIDER", raising=False)
    monkeypatch.delenv("MINIMAX_BASE_URL", raising=False)
    return cfg


def test_the_coding_plan_and_local_models_cost_nothing_per_token(chat):
    assert providers.flat_rate("ollama")
    assert not providers.flat_rate("openai")
    assert not providers.flat_rate("minimax")                       # pay-as-you-go by default
    chat["minimax_base_url"] = brain.MINIMAX_CODING_PLAN_URL
    assert providers.flat_rate("minimax")
    chat["minimax_base_url"] = "https://api.minimaxi.chat/v1"
    assert not providers.flat_rate("minimax")


def test_the_defaults_are_the_chats_provider_and_model(chat):
    chat.update(default_provider="minimax", minimax_base_url=brain.MINIMAX_CODING_PLAN_URL)
    d = providers.session_defaults()
    assert d["provider"] == "minimax"
    assert d["model"] == brain.DEFAULT_MODELS["minimax"]
    assert d["flat_rate"] is True and d["budget_cost_cents"] == providers.DEFAULT_BUDGET_CENTS
    chat["minimax_model"] = "MiniMax-M3-mini"
    assert providers.session_defaults()["model"] == "MiniMax-M3-mini"


def test_nothing_set_up_means_no_default(chat, monkeypatch):
    monkeypatch.setattr(brain, "resolve_provider", lambda *a, **k: brain.MockProvider())
    assert providers.session_defaults()["provider"] is None


def test_a_session_needs_only_the_repository_and_the_branch(api, chat):  # noqa: F811
    chat.update(default_provider="minimax", minimax_base_url=brain.MINIMAX_CODING_PLAN_URL)
    call = api["call"]
    assert call("GET", "/api/sessions/defaults")[1]["provider"] == "minimax"
    call("POST", "/api/sessions/repos", {"path": str(api["repo"])})
    st, r = call("POST", "/api/sessions", {"repo_path": str(api["repo"]), "base_ref": "main"})
    assert st == 201, r
    s = r["session"]
    assert (s["provider"], s["model"], s["key_ref"]) == ("minimax", brain.DEFAULT_MODELS["minimax"], None)
    assert s["role"] == "engineer" and s["budget_cost_cents"] == providers.DEFAULT_BUDGET_CENTS


def _start_one(api, chat_cfg):  # noqa: F811
    """Create a session with only the repository and the branch, give it one
    task and start it."""
    call = api["call"]
    call("POST", "/api/sessions/repos", {"path": str(api["repo"])})
    sid = call("POST", "/api/sessions", {"repo_path": str(api["repo"]), "base_ref": "main"})[1]["session"]["id"]
    api["provs"][sid] = Mock([J(final="ok")])
    tid = call("POST", "/api/sessions/tasks", {"session_id": sid, "title": "t"})[1]["task"]["id"]
    call("POST", f"/api/sessions/{sid}/start")
    return sid, tid


def test_a_flat_rate_session_starts_without_a_price(api, chat):  # noqa: F811
    chat.update(default_provider="minimax", minimax_base_url=brain.MINIMAX_CODING_PLAN_URL)
    call = api["call"]
    sid, tid = _start_one(api, chat)
    _until(lambda: _task(api, tid)["status"] == "needs_review")
    kinds = [e["kind"] for e in call("GET", f"/api/sessions/{sid}/events?limit=500")[1]["items"]]
    assert "budget.price_missing" not in kinds
    assert not [m for m in call("GET", "/api/sessions/inbox")[1]["items"] if "needs a price" in m.get("body", "")]
    call("POST", f"/api/sessions/{sid}/stop")


def test_a_pay_per_token_session_still_asks_for_a_price(api, chat):  # noqa: F811
    """The budget stays real where money is spent per token."""
    chat.update(default_provider="anthropic")
    call = api["call"]
    sid, _ = _start_one(api, chat)
    _until(lambda: call("GET", f"/api/sessions/{sid}")[1]["session"]["status"] == "waiting_human")
    assert call("POST", f"/api/sessions/{sid}/resume")[1]["error"] == "price_missing"
    call("POST", f"/api/sessions/{sid}/stop")


def test_no_ai_set_up_says_so(api, chat, monkeypatch):  # noqa: F811
    monkeypatch.setattr(brain, "resolve_provider", lambda *a, **k: brain.MockProvider())
    call = api["call"]
    call("POST", "/api/sessions/repos", {"path": str(api["repo"])})
    st, r = call("POST", "/api/sessions", {"repo_path": str(api["repo"]), "base_ref": "main"})
    assert st == 400 and r["field"] == "provider" and "Credentials" in r["message"]


def test_the_form_asks_only_for_the_repository_and_the_branch():
    html = (ROOT / "ops_dashboard/web/sessions.html").read_text()
    form = html[html.index('<form id="session-form"'):html.index("</form>", html.index('<form id="session-form"'))]
    required = [line for line in form.splitlines() if " required" in line]
    assert len(required) == 2 and 's-repo"' in required[0] and 's-base"' in required[1]
    assert '<details id="s-advanced">' in form
    js = (ROOT / "ops_dashboard/web/sessions.js").read_text()
    assert "loadDefaults()" in js and "/api/sessions/defaults" in js
    for lang in ("en", "es"):
        d = json.loads((ROOT / f"ops_dashboard/web/i18n/sessions.{lang}.json").read_text())
        root = d.get("sessions", d)
        assert "{0}" in root["uses"] and "{1}" in root["uses_flat"] and root["advanced"]
