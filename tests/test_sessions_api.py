"""Sessions API (routes_sessions) against the REAL local HTTP server, with
scripted MockProviders (no network, no real keys). Every endpoint, validations,
limits, authorization, resume after pause, and that no response ever contains
a fake secret planted in env / keychain stand-in / prompts / provider errors.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import brain, routes_sessions
from ops_dashboard.sessions import providers
from ops_dashboard.sessions import worktrees as W

FAKE = "sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE0123456789"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}
SEEN: list[bytes] = []          # every response body, checked for secrets


def g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV, "HOME": str(cwd)}).stdout


def J(tool=None, final=None, **args):
    return json.dumps({"final": final} if final is not None else {"tool": tool, "args": args})


class Mock(brain.MockProvider):
    """brain.MockProvider with a script; records what the model was sent."""

    def __init__(self, replies, *, default=None, usage=None, error=None):
        self.model = "m"
        self.replies, self.default, self.usage, self.error = list(replies), default, usage, error
        self.calls: list[list[dict]] = []

    def ask(self, system, messages, **kw):
        self.calls.append([dict(m) for m in messages])
        if self.error:
            raise self.error
        self.last_usage = self.usage
        if self.replies:
            return self.replies.pop(0)
        return self.default or J(final="done")


@pytest.fixture()
def api(tmp_path, monkeypatch):
    dbp = tmp_path / "api.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    monkeypatch.setenv(W.WORKTREES_ENV, str(tmp_path / "wt"))
    monkeypatch.setenv("OPENAI_API_KEY", FAKE)                 # planted secret
    from ops_dashboard import db, secrets_store
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(secrets_store, "get_secret", lambda name: FAKE)
    db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    g(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("V = 1\n")
    g(repo, "add", ".")
    g(repo, "commit", "-q", "-m", "init")
    provs: dict[int, Mock] = {}
    default = {"p": None}

    def factory(s):
        return provs.get(s["id"]) or default["p"] or Mock([])

    providers.reset_cooldowns()
    routes_sessions.configure(provider_factory=factory, tools_kwargs={"python": sys.executable})
    from ops_dashboard.server import DashboardHandler

    class Quiet(DashboardHandler):
        def log_message(self, *a):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def call(method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        SEEN.append(raw)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, {"raw": raw.decode("utf-8", "replace")}

    yield {"call": call, "port": port, "repo": repo, "provs": provs, "default": default, "db": db,
           "tmp": tmp_path}
    routes_sessions.configure()
    server.shutdown()
    providers.reset_cooldowns()


def _setup(api, *, provider="openai", key_ref="byok.openai.work", budget=500, price=True,
           **extra):
    call = api["call"]
    assert call("POST", "/api/sessions/repos", {"path": str(api["repo"])})[0] == 201
    if price:
        assert call("POST", "/api/sessions/prices", {"provider": provider, "model": "m",
                                                      "in_cents_per_mtok": 0,
                                                      "out_cents_per_mtok": 10_000})[0] == 200
    body = {"role": "backend", "provider": provider, "model": "m", "key_ref": key_ref,
            "repo_path": str(api["repo"]), "base_ref": "main", "budget_cost_cents": budget, **extra}
    if key_ref is None:
        body.pop("key_ref")
    st, r = call("POST", "/api/sessions", body)
    assert st == 201, r
    return r["session"]["id"]


def _until(fn, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def _task(api, tid):
    st, r = api["call"]("GET", f"/api/sessions/tasks?after_id={tid - 1}&limit=1")
    return r["items"][0]


# ── repos & prices ─────────────────────────────────────────────────────────

def test_repos_mark_list_unmark_and_validation(api):
    call, repo = api["call"], api["repo"]
    plain = api["tmp"] / "plain"
    plain.mkdir()
    assert call("POST", "/api/sessions/repos", {"path": str(api["tmp"] / "nope")})[1]["error"] \
        == "validation_error"
    assert call("POST", "/api/sessions/repos", {"path": str(plain)})[1]["error"] == "not_a_repo"
    (repo / "sub").mkdir()
    st, r = call("POST", "/api/sessions/repos", {"path": str(repo / "sub")})
    assert st == 400 and r["error"] == "not_a_repo"              # only the repo root
    assert call("POST", "/api/sessions/repos", {})[1]["field"] == "path"
    st, r = call("POST", "/api/sessions/repos", {"path": str(repo)})
    assert st == 201 and r["repo_path"] == os.path.realpath(repo)
    items = call("GET", "/api/sessions/repos")[1]["items"]
    assert items[0]["enabled"] == 1
    sid = _setup(api)
    assert call("POST", "/api/sessions/repos/unmark", {"path": str(repo)})[1]["error"] \
        == "invalid_state"                                           # active session on it
    call("POST", f"/api/sessions/{sid}/close")
    st, r = call("POST", "/api/sessions/repos/unmark", {"path": str(repo)})
    assert st == 200 and r["enabled"] is False
    assert call("POST", "/api/sessions/repos/unmark",
                {"path": str(plain)})[0] == 404


def test_prices(api):
    call = api["call"]
    assert call("POST", "/api/sessions/prices", {"provider": "nope", "model": "m",
                                                  "in_cents_per_mtok": 1,
                                                  "out_cents_per_mtok": 1})[0] == 400
    assert call("POST", "/api/sessions/prices", {"provider": "openai", "model": "m",
                                                  "in_cents_per_mtok": -1,
                                                  "out_cents_per_mtok": 1})[1]["field"] \
        == "in_cents_per_mtok"
    assert call("POST", "/api/sessions/prices", {"provider": "openai", "model": "m",
                                                  "in_cents_per_mtok": 1, "out_cents_per_mtok": 2,
                                                  "currency": "USD"})[0] == 400
    st, r = call("POST", "/api/sessions/prices", {"provider": "openai", "model": "m",
                                                   "in_cents_per_mtok": 250,
                                                   "out_cents_per_mtok": 1000})
    assert st == 200 and r["price"]["out_cents_per_mtok"] == 1000
    assert call("GET", "/api/sessions/prices")[1]["currency"] == "EUR"


# ── sessions: create/validation/limits ─────────────────────────────────────

def test_create_session_validations_and_limits(api):
    call, repo = api["call"], str(api["repo"])
    base = {"role": "be", "provider": "openai", "model": "m", "key_ref": "byok.openai.work",
            "repo_path": repo, "base_ref": "main", "budget_cost_cents": 100}
    st, r = call("POST", "/api/sessions", base)
    assert st == 400 and r["field"] == "repo_path"                     # repo not marked
    call("POST", "/api/sessions/repos", {"path": repo})
    for missing in ("role", "provider", "model", "repo_path", "base_ref", "budget_cost_cents",
                    "key_ref"):
        b = dict(base)
        b.pop(missing)
        st, r = call("POST", "/api/sessions", b)
        assert st == 400 and r["error"] == "validation_error" and r["field"] == missing, missing
    for bad in ({"budget_cost_cents": 0}, {"budget_cost_cents": "10"}, {"budget_cost_cents": 1.5},
                {"max_iterations": 61}, {"max_iterations": 0}, {"provider": "evil"},
                {"base_ref": "--upload-pack=x"}, {"scope_globs": "x"}):
        st, r = call("POST", "/api/sessions", {**base, **bad})
        assert st == 400, (bad, r)
    st, r = call("POST", "/api/sessions", {**base, "key_ref": FAKE})   # a key instead of a ref
    assert st == 400 and r["field"] == "key_ref" and FAKE not in r["message"]
    st, r = call("POST", "/api/sessions", {**base, "provider": "anthropic"})  # OpenAI key, Anthropic
    assert st == 400 and r["field"] == "key_ref" and "belongs to provider 'openai'" in r["message"]
    st, r = call("POST", "/api/sessions", {**base, "base_ref": "no-such-branch"})
    assert st == 400 and r["error"] == "worktree_error"
    # ollama needs no key_ref; 3 active sessions by default (one failed above isn't active)
    ids = []
    for i in range(3):
        st, r = call("POST", "/api/sessions", {**base, "provider": "ollama", "key_ref": None,
                                               "role": f"r{i}"})
        assert st == 201, r
        ids.append(r["session"]["id"])
        assert r["session"]["key_ref"] is None and r["session"]["branch"] == f"cl/r{i}/{ids[-1]}"
    st, r = call("POST", "/api/sessions", {**base, "role": "r4"})
    assert st == 409 and r["error"] == "limit_reached"
    lst = call("GET", "/api/sessions?active=1")[1]
    assert len(lst["items"]) == 3 and lst["max_running"] == 3
    assert call("GET", "/api/sessions/999")[0] == 404
    assert call("GET", "/api/sessions/abc")[0] == 404


# ── full flow: tasks, run, messages, events, diff, review ──────────────────

def test_full_flow_task_run_review_accept_never_merges(api):
    call = api["call"]
    sid = _setup(api)
    api["provs"][sid] = Mock([
        J("claim", patterns=["feature.py"]),
        J("write_file", path="feature.py", content="def f():\n    return 42\n"),
        J(final="added f"),
    ], usage={"input_tokens": 10, "output_tokens": 20, "source": "input_output"})
    assert call("POST", "/api/sessions/tasks", {"session_id": 999, "title": "x"})[0] == 400
    assert call("POST", "/api/sessions/tasks", {"session_id": sid})[1]["field"] == "title"
    assert call("POST", "/api/sessions/tasks", {"session_id": sid, "title": "x",
                                                "paths_hint": "a"})[0] == 400
    st, r = call("POST", "/api/sessions/tasks", {"session_id": sid, "title": "feature",
                                                 "prompt": "add f()", "paths_hint": ["feature.py"]})
    assert st == 201 and r["task"]["assigned_session_id"] == sid
    tid = r["task"]["id"]
    st, r = call("POST", f"/api/sessions/{sid}/start")
    assert st == 200
    assert call("POST", f"/api/sessions/{sid}/start")[1]["error"] == "invalid_state"
    _until(lambda: _task(api, tid)["status"] == "needs_review")
    st, d = call("GET", f"/api/sessions/{sid}/diff")
    assert st == 200 and "+    return 42" in d["diff"] and "feature.py" in d["stat"]
    st, det = call("GET", f"/api/sessions/{sid}")
    assert det["session"]["usage_includes_estimates"] is False
    assert det["task"]["id"] == tid and det["worktree"]["ahead"] == 1
    # events paginate
    st, p1 = call("GET", f"/api/sessions/{sid}/events?limit=3")
    assert len(p1["items"]) == 3
    st, p2 = call("GET", f"/api/sessions/{sid}/events?after_id={p1['next_after_id']}&limit=500")
    assert p2["items"][0]["id"] > p1["items"][-1]["id"]
    assert any(e["kind"] == "integration.dry_merge" for e in p2["items"])
    assert call("GET", f"/api/sessions/{sid}/events?limit=0")[0] == 400
    # review
    assert call("POST", f"/api/sessions/tasks/{tid}/review", {"decision": "maybe"})[0] == 400
    st, r = call("POST", f"/api/sessions/tasks/{tid}/review", {"decision": "accept", "note": "lgtm"})
    assert st == 200 and r["merged"] is False and r["task"]["status"] == "done"
    assert call("POST", f"/api/sessions/tasks/{tid}/review",
                {"decision": "accept"})[1]["error"] == "invalid_state"
    assert g(api["repo"], "rev-list", "--count", "main").strip() == "1"   # nothing merged
    assert call("GET", f"/api/sessions/claims?session_id={sid}")[1]["items"] == []
    call("POST", f"/api/sessions/{sid}/stop")


def test_reject_review_and_user_messages_reach_the_model(api):
    call = api["call"]
    sid = _setup(api)
    seen = {}

    class Listening(Mock):
        def ask(self, system, messages, **kw):
            seen.setdefault("msgs", []).append(messages[-1]["content"])
            return super().ask(system, messages, **kw)

    prov = Listening([J("list_dir")], default=J(final="ok"))
    api["provs"][sid] = prov
    assert call("POST", f"/api/sessions/{sid}/messages", {"body": ""})[0] == 400
    st, r = call("POST", f"/api/sessions/{sid}/messages", {"body": "please use snake_case"})
    assert st == 201
    tid = call("POST", "/api/sessions/tasks", {"session_id": sid, "title": "t"})[1]["task"]["id"]
    call("POST", f"/api/sessions/{sid}/start")
    _until(lambda: _task(api, tid)["status"] == "needs_review")
    assert any("please use snake_case" in m for m in seen["msgs"])
    msgs = call("GET", f"/api/sessions/{sid}/messages")[1]["items"]
    assert msgs[0]["body"] == "please use snake_case" and msgs[0]["read_at"]
    st, r = call("POST", f"/api/sessions/tasks/{tid}/review", {"decision": "reject",
                                                               "note": "wrong approach"})
    assert r["task"]["status"] == "failed" and "rejected" in r["task"]["result_summary"]
    call("POST", f"/api/sessions/{sid}/stop")


def test_cancel_task_and_claims_release(api):
    call = api["call"]
    sid = _setup(api)
    api["provs"][sid] = Mock([J("claim", patterns=["wip.py"])], default=J("list_dir"))
    tid = call("POST", "/api/sessions/tasks", {"session_id": sid, "title": "t"})[1]["task"]["id"]
    queued = call("POST", "/api/sessions/tasks", {"session_id": sid, "title": "later"})[1]["task"]["id"]
    st, r = call("POST", f"/api/sessions/tasks/{queued}/cancel")
    assert st == 200 and r["task"]["status"] == "cancelled"
    assert call("POST", f"/api/sessions/tasks/{queued}/cancel")[1]["error"] == "invalid_state"
    call("POST", f"/api/sessions/{sid}/start")
    _until(lambda: call("GET", f"/api/sessions/claims?session_id={sid}")[1]["items"])
    claims = call("GET", f"/api/sessions/claims?session_id={sid}")[1]["items"]
    assert claims[0]["pattern"] == "wip.py"
    st, r = call("POST", f"/api/sessions/tasks/{tid}/cancel")     # stops the session cleanly
    assert st == 200 and r["task"]["status"] == "cancelled"
    assert call("GET", f"/api/sessions/claims?session_id={sid}")[1]["items"] == []
    assert call("POST", "/api/sessions/tasks/abc/cancel")[0] == 404
    # manual release endpoint
    assert call("POST", "/api/sessions/claims/release", {"claim_ids": [1]})[1]["field"] \
        == "session_id"
    assert call("POST", "/api/sessions/claims/release", {"session_id": sid})[1]["released"] == 0


# ── pause → resume ─────────────────────────────────────────────────────────

def test_resume_after_budget_pause_requires_raising_it(api):
    call = api["call"]
    sid = _setup(api, budget=100)
    # 40 c per turn: 40 → 80 → 120 → paused.
    api["provs"][sid] = Mock([J("list_dir")] * 3 + [J(final="finished")],
                             usage={"input_tokens": 0, "output_tokens": 4000, "source": "x"})
    tid = call("POST", "/api/sessions/tasks", {"session_id": sid, "title": "t"})[1]["task"]["id"]
    call("POST", f"/api/sessions/{sid}/start")
    _until(lambda: call("GET", f"/api/sessions/{sid}")[1]["session"]["status"] == "paused")
    st, r = call("POST", f"/api/sessions/{sid}/resume")
    assert st == 409 and r["error"] == "budget_exhausted"
    st, r = call("POST", f"/api/sessions/{sid}/resume", {"budget_cost_cents": 50})
    assert st == 400 and r["field"] == "budget_cost_cents"         # below what was spent
    st, r = call("POST", f"/api/sessions/{sid}/resume", {"budget_cost_cents": 1000})
    assert st == 200, r
    _until(lambda: _task(api, tid)["status"] == "needs_review")
    kinds = [e["kind"] for e in call("GET", f"/api/sessions/{sid}/events?limit=500")[1]["items"]]
    assert "session.resumed" in kinds and "budget.warning" in kinds
    call("POST", f"/api/sessions/{sid}/stop")


def test_resume_after_missing_price(api):
    call = api["call"]
    sid = _setup(api, provider="anthropic", key_ref="byok.anthropic.main", price=False)
    api["provs"][sid] = Mock([J(final="ok")])
    tid = call("POST", "/api/sessions/tasks", {"session_id": sid, "title": "t"})[1]["task"]["id"]
    call("POST", f"/api/sessions/{sid}/start")
    _until(lambda: call("GET", f"/api/sessions/{sid}")[1]["session"]["status"] == "waiting_human")
    inbox = call("GET", "/api/sessions/inbox")[1]["items"]
    assert inbox and inbox[0]["kind"] == "request"
    assert call("POST", f"/api/sessions/{sid}/resume")[1]["error"] == "price_missing"
    call("POST", "/api/sessions/prices", {"provider": "anthropic", "model": "m",
                                          "in_cents_per_mtok": 300, "out_cents_per_mtok": 1500})
    assert call("POST", f"/api/sessions/{sid}/resume")[0] == 200
    _until(lambda: _task(api, tid)["status"] == "needs_review")
    call("POST", f"/api/sessions/{sid}/stop")


def test_stalled_resumes_only_with_explicit_confirmation(api):
    call = api["call"]
    sid = _setup(api)
    tid = call("POST", "/api/sessions/tasks", {"session_id": sid, "title": "t"})[1]["task"]["id"]
    st = routes_sessions.supervisor().store
    st.claim_task(sid)
    st.set_task_status(tid, sid, "running")
    st.set_session_status(sid, "running")
    routes_sessions.configure(provider_factory=lambda s: Mock([J(final="resumed ok")]),
                              tools_kwargs={"python": sys.executable})   # "process restart"
    s = call("GET", f"/api/sessions/{sid}")[1]["session"]
    assert s["status"] == "stalled" and s["running"] is False          # never relaunched
    assert call("POST", f"/api/sessions/{sid}/start")[1]["error"] == "invalid_state"
    assert call("POST", f"/api/sessions/{sid}/resume")[1]["error"] == "confirm_required"
    assert call("POST", f"/api/sessions/{sid}/resume", {"confirm_stalled": "yes"})[0] == 409
    assert call("POST", f"/api/sessions/{sid}/resume", {"confirm_stalled": True})[0] == 200
    _until(lambda: _task(api, tid)["status"] == "needs_review")
    call("POST", f"/api/sessions/{sid}/stop")


def test_close_returns_task_and_keeps_worktree(api):
    call = api["call"]
    sid = _setup(api)
    det = call("GET", f"/api/sessions/{sid}")[1]
    wt = det["worktree"]["path"]
    st, r = call("POST", f"/api/sessions/{sid}/close")
    assert st == 200 and r["session"]["status"] == "closed" and os.path.isdir(wt)
    assert call("POST", f"/api/sessions/{sid}/close")[1]["error"] == "invalid_state"
    assert call("POST", f"/api/sessions/{sid}/messages", {"body": "hi"})[0] == 409


# ── errors, authorization, origin, secrets ─────────────────────────────────

def test_stable_json_errors(api):
    call = api["call"]
    st, r = call("POST", "/api/sessions", None)
    assert st == 400 and r["ok"] is False and r["error"] == "validation_error"
    st, r = call("POST", "/api/sessions/tasks/1/review", {"decision": "accept"})
    assert st == 404 and r == {"ok": False, "error": "not_found", "code": "not_found",
                               "message": "no such task"}
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", _port_of(api))
    conn.request("POST", "/api/sessions/prices", body=b"{not json",
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 400 and json.loads(resp.read())["error"] == "invalid_json"


def _port_of(api):
    return api["port"]


def test_remote_requests_need_auth_and_local_owner_does_not(api):
    call = api["call"]
    remote = {"X-Forwarded-For": "203.0.113.9"}
    assert call("GET", "/api/sessions", headers=remote)[0] == 401
    assert call("GET", "/api/sessions/repos", headers=remote)[0] == 401
    assert call("POST", "/api/sessions/prices", {"provider": "openai", "model": "m",
                                                  "in_cents_per_mtok": 1,
                                                  "out_cents_per_mtok": 1}, headers=remote)[0] == 401
    token = api["db"].create_device_token("phone")
    auth = {**remote, "Authorization": f"Bearer {token}"}
    assert call("GET", "/api/sessions", headers=auth)[0] == 200
    assert call("POST", "/api/sessions/prices", {"provider": "openai", "model": "m",
                                                  "in_cents_per_mtok": 1,
                                                  "out_cents_per_mtok": 1}, headers=auth)[0] == 200
    assert call("GET", "/api/sessions",
                headers={**remote, "Authorization": "Bearer wrong"})[0] == 401
    assert call("GET", "/api/sessions")[0] == 200                  # loopback owner


def test_cross_site_browser_requests_are_refused(api):
    call = api["call"]
    for h in ({"Origin": "https://evil.example"}, {"Origin": "null"},
              {"Sec-Fetch-Site": "cross-site"}):
        st, r = call("POST", "/api/sessions/prices", {"provider": "openai", "model": "m",
                                                       "in_cents_per_mtok": 1,
                                                       "out_cents_per_mtok": 1}, headers=h)
        assert st == 403 and r["error"] == "forbidden_origin", h
        assert call("GET", "/api/sessions", headers=h)[0] == 403
    assert call("GET", "/api/sessions/prices")[1]["items"] == []    # nothing was written


def test_provider_unavailable_error_shape_and_no_secret_anywhere(api):
    call = api["call"]
    sid = _setup(api)
    from ops_dashboard.brain import ProviderError
    api["provs"][sid] = Mock([], error=ProviderError("invalid_key", f"bad key {FAKE}", 401))
    tid = call("POST", "/api/sessions/tasks", {"session_id": sid, "title": "t",
                                               "prompt": f"my key is {FAKE} use it"})[1]["task"]["id"]
    call("POST", f"/api/sessions/{sid}/messages", {"body": f"token {FAKE}"})
    call("POST", f"/api/sessions/{sid}/start")
    _until(lambda: call("GET", f"/api/sessions/{sid}")[1]["session"]["status"] == "waiting_human")
    # every read endpoint, then assert no response body seen in this module had the secret
    for path in ("/api/sessions", f"/api/sessions/{sid}", f"/api/sessions/{sid}/events?limit=500",
                 f"/api/sessions/{sid}/messages", "/api/sessions/inbox",
                 f"/api/sessions/tasks?session_id={sid}", "/api/sessions/claims",
                 "/api/sessions/repos", "/api/sessions/prices", f"/api/sessions/{sid}/diff"):
        assert call("GET", path)[0] == 200, path
    assert _task(api, tid)["prompt"].startswith("my key is [redacted-secret]")
    # provider_unavailable mapping keeps #15's shape
    def unavailable(s):
        raise providers.ProviderUnavailable("openai", "cooldown", "cooling down", 42)
    other = _setup2(api)
    routes_sessions.supervisor().provider_factory = unavailable
    st, r = call("POST", f"/api/sessions/{other}/start")
    assert st == 503 and r["error"] == "provider_unavailable" and r["provider"] == "openai"
    assert r["retry_after"] == 42 and r["reason"] == "cooldown" and r["ok"] is False
    leaked = [b for b in SEEN if FAKE.encode() in b]
    assert leaked == []
    call("POST", f"/api/sessions/{sid}/stop")


def _setup2(api):
    st, r = api["call"]("POST", "/api/sessions", {
        "role": "second", "provider": "openai", "model": "m", "key_ref": "byok.openai.work",
        "repo_path": str(api["repo"]), "base_ref": "main", "budget_cost_cents": 100})
    assert st == 201, r
    return r["session"]["id"]


def test_no_response_in_this_module_leaked_the_fake_secret():
    assert SEEN and not [b for b in SEEN if FAKE.encode() in b]
