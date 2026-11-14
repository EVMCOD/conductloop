"""Supervisor + runner with scripted mock providers (no network, no keys).

Covers: full session over several user-assigned tasks, the iteration cap,
EUR budget 80 % / 100 %, missing price, Ollama serialised across sessions,
commands refused without an OS sandbox, restart recovery (stalled, never
relaunched), heartbeats, clean cancellation, dry-merge conflict → blocked,
provider errors + per-key cooldown, and the provider adapter.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from ops_dashboard.sessions import SessionStore, providers, sandbox
from ops_dashboard.sessions import worktrees as W
from ops_dashboard.sessions.runner import SessionRunner, parse_reply
from ops_dashboard.sessions.supervisor import Supervisor, SupervisorError

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}


def g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV, "HOME": str(cwd)}).stdout


def J(tool=None, final=None, **args):
    return json.dumps({"final": final} if final is not None else {"tool": tool, "args": args})


class Scripted:
    """Replies from a list (then ``default``); records what it was sent."""

    def __init__(self, replies, *, name="openai", default=None, usage=None, delay=0.0,
                 tracker=None):
        self.name, self.model = name, "m"
        self.replies, self.default = list(replies), default
        self.usage, self.delay, self.tracker = usage, delay, tracker
        self.calls: list[list[dict]] = []
        self.last_usage = None

    def ask(self, system, messages, **kw):
        if self.tracker:
            self.tracker.enter()
        try:
            time.sleep(self.delay)
            self.calls.append([dict(m) for m in messages])
            self.last_usage = self.usage
            if self.replies:
                return self.replies.pop(0)
            if self.default is None:
                return J(final="nothing left")
            return self.default
        finally:
            if self.tracker:
                self.tracker.leave()


class Tracker:
    def __init__(self):
        self.lock, self.now, self.max = threading.Lock(), 0, 0

    def enter(self):
        with self.lock:
            self.now += 1
            self.max = max(self.max, self.now)

    def leave(self):
        with self.lock:
            self.now -= 1


@pytest.fixture(autouse=True)
def _reset_cooldowns():
    providers.reset_cooldowns()
    yield
    providers.reset_cooldowns()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "s.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    g(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n")
    g(repo, "add", ".")
    g(repo, "commit", "-q", "-m", "init")
    monkeypatch.setenv(W.WORKTREES_ENV, str(tmp_path / "wt-root"))
    store = SessionStore()
    store.mark_repo(str(repo))
    store.set_model_price("openai", "m", in_cents_per_mtok=0, out_cents_per_mtok=10_000)
    provs: dict[int, Scripted] = {}

    def session(name="s", *, provider="openai", model="m", budget=10_000, max_iterations=60,
                prov=None):
        sid = store.create_session(name=name, role=name, provider=provider, model=model,
                                   repo_path=str(repo), base_ref="main", budget_cost_cents=budget,
                                   max_iterations=max_iterations)
        info = W.create(repo, "main", session_id=sid, role=name)
        store.set_session_worktree(sid, info.path, info.branch)
        if prov is not None:
            provs[sid] = prov
        return sid

    def task(sid, title="t", prompt="do it"):
        return store.enqueue_task(title=title, prompt=prompt, assigned_session_id=sid)

    def factory(s):
        return provs[s["id"]]

    sup = Supervisor(store, provider_factory=factory, poll_s=0.05,
                     tools_kwargs={"python": sys.executable})
    yield {"store": store, "repo": repo, "session": session, "task": task, "sup": sup,
           "provs": provs, "factory": factory}
    sup.shutdown(timeout=10)


def until(pred, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def kinds(store, sid):
    return [e["kind"] for e in store.list_events(sid, limit=1000)]


# ── full session ───────────────────────────────────────────────────────────

def test_full_session_runs_assigned_tasks_and_never_merges(env):
    store, repo = env["store"], env["repo"]
    prov = Scripted([
        J("claim", patterns=["src/feature.py"]),
        J("write_file", path="src/feature.py", content="def f():\n    return 1\n"),
        J("diff"),
        J(final="added feature"),
        J("claim", patterns=["src/feature.py"]),
        J("edit_file", path="src/feature.py", old="return 1", new="return 2"),
        J(final="tweaked feature"),
    ])
    sid = env["session"]("backend", prov=prov)
    other = env["session"]("other", prov=Scripted([]))
    t1 = env["task"](sid, "feature")
    t_other = env["task"](other, "not yours")
    t_unassigned = store.enqueue_task(title="nobody", prompt="x")
    env["sup"].start(sid)
    until(lambda: store.get_task(t1)["status"] == "needs_review")
    # D8: only the user-assigned task was taken.
    assert store.get_task(t_other)["owner_session_id"] is None
    assert store.get_task(t_unassigned)["owner_session_id"] is None
    # the user reviews task 1, assigns task 2
    assert store.set_task_status(t1, sid, "done")
    t2 = env["task"](sid, "tweak")
    until(lambda: store.get_task(t2)["status"] == "needs_review")
    env["sup"].stop(sid)
    s = store.get_session(sid)
    log = g(repo, "log", "--format=%an <%ae>|%s", s["branch"])
    assert log.count("ConductLoop agent <agent@conductloop.invalid>") == 2
    assert "cl: task #" in log
    # never merged / pushed: main untouched, still one commit, no remotes
    assert g(repo, "rev-list", "--count", "main").strip() == "1"
    assert g(repo, "remote").strip() == ""
    k = kinds(store, sid)
    for kind in ("session.started", "task.started", "llm.turn", "tool.claim", "tool.write",
                 "tool.edit", "tool.diff", "integration.dry_merge"):
        assert kind in k
    assert store.get_task(t2)["result_summary"] == "tweaked feature"


def test_parse_reply_tolerates_fences_and_prose():
    assert parse_reply('Sure!\n```json\n{"final": "x"}\n```') == {"final": "x"}
    assert parse_reply('ok {"tool": "diff", "args": {}} trailing') == {"tool": "diff", "args": {}}
    assert parse_reply("no json") is None


def test_invalid_replies_are_fed_back_not_fatal(env):
    store = env["store"]
    prov = Scripted(["I think I will…", J(final="done")])
    sid = env["session"]("a", prov=prov)
    tid = env["task"](sid)
    out = SessionRunner(store, sid, provider_factory=env["factory"]).run_task(store.claim_task(sid))
    assert out.task_status == "needs_review"
    assert "reply with one JSON object" in prov.calls[1][-1]["content"]
    assert store.get_task(tid)["status"] == "needs_review"


# ── limits ─────────────────────────────────────────────────────────────────

def test_iteration_cap_blocks_task_and_waits_for_human(env):
    store = env["store"]
    prov = Scripted([], default=J("list_dir", path="."))
    sid = env["session"]("a", max_iterations=3, prov=prov)
    tid = env["task"](sid)
    out = SessionRunner(store, sid, provider_factory=env["factory"]).run_task(store.claim_task(sid))
    assert len(prov.calls) == 3 and out.iterations == 3
    # The task stays in flight, so raising max_iterations and resuming continues it.
    assert store.get_task(tid)["status"] == "running"
    assert store.in_flight_task(sid)["id"] == tid
    assert store.get_session(sid)["status"] == "waiting_human"
    assert "limit.iterations" in kinds(store, sid)


def test_budget_warns_at_80_and_pauses_at_100(env):
    store = env["store"]
    # 4000 output tokens × 10 000 c/Mtok = 40 cents per turn; budget 100 cents.
    prov = Scripted([], default=J("list_dir"), usage={"input_tokens": 0, "output_tokens": 4000})
    sid = env["session"]("a", budget=100, prov=prov)
    tid = env["task"](sid)
    out = SessionRunner(store, sid, provider_factory=env["factory"]).run_task(store.claim_task(sid))
    k = kinds(store, sid)
    assert k.count("budget.warning") == 1
    assert k.index("budget.warning") < k.index("budget.exhausted")
    assert len(prov.calls) == 3                    # 40 → 80 (warn) → 120 (pause)
    s = store.get_session(sid)
    assert s["status"] == "paused" and out.session_status == "paused"
    assert store.get_task(tid)["status"] == "running"   # kept for the human, not lost


def test_budget_pauses_before_a_call_projected_to_cross_it(env):
    store = env["store"]
    prov = Scripted([], default=J("list_dir"))
    sid = env["session"]("a", budget=10, prov=prov)   # projected 1500 out × 10 000 = 15 c
    env["task"](sid)
    SessionRunner(store, sid, provider_factory=env["factory"]).run_task(store.claim_task(sid))
    assert prov.calls == []                            # never spent a cent
    assert store.get_session(sid)["status"] == "paused"
    ev = [e for e in store.list_events(sid) if e["kind"] == "budget.exhausted"][0]
    assert json.loads(ev["payload"])["projected"] is True


def test_missing_price_pauses_and_asks_before_any_call(env):
    store = env["store"]
    prov = Scripted([J(final="x")])
    sid = env["session"]("a", provider="anthropic", model="unpriced", prov=prov)
    tid = env["task"](sid)
    SessionRunner(store, sid, provider_factory=env["factory"]).run_task(store.claim_task(sid))
    assert prov.calls == []
    assert store.get_session(sid)["status"] == "waiting_human"
    assert store.get_task(tid)["status"] == "running"
    msgs = store.list_messages(None)
    assert msgs and msgs[0]["kind"] == "request" and "price" in msgs[0]["body"]
    assert "budget.price_missing" in kinds(store, sid)


def test_ollama_costs_zero_without_price(env):
    store = env["store"]
    prov = Scripted([J("list_dir"), J(final="ok")], name="ollama",
                    usage={"input_tokens": 10_000, "output_tokens": 10_000})
    sid = env["session"]("a", provider="ollama", model="llama", budget=1, prov=prov)
    tid = env["task"](sid)
    SessionRunner(store, sid, provider_factory=env["factory"]).run_task(store.claim_task(sid))
    assert store.get_task(tid)["status"] == "needs_review"
    assert store.get_session(sid)["cost_cents_est"] == 0


def test_ollama_is_serialised_across_sessions(env):
    sup = env["sup"]
    tracker = Tracker()
    sids = []
    for i in range(3):
        prov = Scripted([J("list_dir"), J(final="ok")], name="ollama", delay=0.15,
                        tracker=tracker)
        sid = env["session"](f"o{i}", provider="ollama", model="llama", prov=prov)
        env["task"](sid)
        sids.append(sid)
    for sid in sids:
        sup.start(sid, stop_when_idle=True)
    for sid in sids:
        sup.wait(sid, timeout=30)
    assert tracker.max == 1
    assert all(o.task_status == "needs_review" for sid in sids for o in sup.wait(sid))


def test_max_running_sessions(env):
    store = env["store"]
    sup = Supervisor(store, provider_factory=env["factory"], max_running=1, poll_s=0.05)
    a = env["session"]("a", prov=Scripted([]))
    b = env["session"]("b", prov=Scripted([]))
    sup.start(a)
    from ops_dashboard.sessions import SessionLimitError, SessionValidationError
    with pytest.raises(SessionLimitError):
        sup.start(b)
    sup.shutdown()
    with pytest.raises(SessionValidationError):
        Supervisor(store, max_running=9)
    assert Supervisor(store).max_running == 3 and Supervisor(store, max_running=8).max_running == 8


# ── sandbox ────────────────────────────────────────────────────────────────

def test_runner_never_runs_unsandboxed_and_tells_the_model(env, monkeypatch):
    store = env["store"]
    monkeypatch.setattr(sandbox, "detect_backend", lambda: None)
    prov = Scripted([J("run", argv=["pytest", "-q"]), J(final="could not test")])
    sid = env["session"]("a", prov=prov)
    tid = env["task"](sid)
    # even if a caller asks for it, the runner drops allow_unsandboxed
    r = SessionRunner(store, sid, provider_factory=env["factory"],
                      tools_kwargs={"allow_unsandboxed": True, "python": sys.executable})
    r.run_task(store.claim_task(sid))
    fed_back = prov.calls[1][-1]["content"]
    assert fed_back.startswith("TOOL_RESULT run:") and "no OS sandbox" in fed_back
    assert '"ok": false' in fed_back
    assert store.get_task(tid)["status"] == "needs_review"
    run_ev = [e for e in store.list_events(sid) if e["kind"] == "tool.run"][0]
    assert json.loads(run_ev["payload"])["denied"] is True


# ── restart, heartbeat, cancellation ───────────────────────────────────────

def test_restart_marks_hung_sessions_stalled_and_never_relaunches(env):
    store = env["store"]
    sid = env["session"]("a", prov=Scripted([]))
    tid = env["task"](sid)
    store.claim_task(sid)
    store.set_task_status(tid, sid, "running")
    store.set_session_status(sid, "running")            # previous process died here
    idle = env["session"]("b", prov=Scripted([]))
    store.set_session_status(idle, "idle")
    sup = Supervisor(store, provider_factory=env["factory"])  # "new process"
    assert sup.recover() == [sid]
    s = store.get_session(sid)
    assert s["status"] == "stalled" and "not relaunched" in s["last_error"]
    assert store.get_task(tid)["status"] == "running"   # owner kept: a human decides
    assert store.get_session(idle)["status"] == "idle"
    assert sup.running() == []
    with pytest.raises(SupervisorError):
        sup.start(sid)
    assert "session.stalled" in kinds(store, sid)


def test_stale_heartbeat_marks_stalled(env):
    store = env["store"]
    sid = env["session"]("a", prov=Scripted([]))
    store.set_session_status(sid, "running")
    store.heartbeat(sid)
    sup = Supervisor(store, provider_factory=env["factory"], stale_after_s=60,
                     clock=lambda: time.time() + 3600)
    assert sup.check_heartbeats() == [sid]
    assert store.get_session(sid)["status"] == "stalled"


def test_clean_cancellation_gives_the_task_back(env):
    store, sup = env["store"], env["sup"]
    prov = Scripted([J("claim", patterns=["src/wip.py"]),
                     J("write_file", path="src/wip.py", content="wip\n")],
                    default=J("list_dir"), delay=0.05)
    sid = env["session"]("a", prov=prov)
    tid = env["task"](sid)
    sup.start(sid)
    until(lambda: len(prov.calls) >= 4)
    assert sup.stop(sid, timeout=10) is True
    t, s = store.get_task(tid), store.get_session(sid)
    assert t["status"] == "queued" and t["owner_session_id"] is None
    assert store.active_claims(session_id=sid) == []
    assert s["status"] == "idle"
    assert (W.Path(s["worktree_path"]) / "src/wip.py").read_text() == "wip\n"  # work kept
    k = kinds(store, sid)
    assert "session.idle" in k and "session.stopped" in k


# ── integration (D6) ───────────────────────────────────────────────────────

def test_dry_merge_conflict_blocks_the_task(env):
    store, repo = env["store"], env["repo"]
    prov = Scripted([J("claim", patterns=["src/app.py"]),
                     J("edit_file", path="src/app.py", old="VALUE = 1", new="VALUE = 2"),
                     J(final="changed value")])
    sid = env["session"]("a", prov=prov)
    tid = env["task"](sid)
    (repo / "src" / "app.py").write_text("VALUE = 3\n")   # meanwhile on main
    g(repo, "commit", "-qam", "main moves on")
    main_head = g(repo, "rev-parse", "main").strip()
    out = SessionRunner(store, sid, provider_factory=env["factory"]).run_task(store.claim_task(sid))
    assert out.task_status == "blocked" and out.conflicts == ["src/app.py"]
    t = store.get_task(tid)
    assert t["status"] == "blocked" and "conflicts in src/app.py" in t["result_summary"]
    assert [m["kind"] for m in store.list_messages(None)] == ["conflict"]
    assert g(repo, "rev-parse", "main").strip() == main_head   # nothing merged
    assert (repo / "src" / "app.py").read_text() == "VALUE = 3\n"
    assert store.get_session(sid)["status"] == "idle"        # free for the next task


# ── providers ──────────────────────────────────────────────────────────────

class Boom:
    name, model = "openai", "m"

    def __init__(self):
        self.calls = 0

    def ask(self, *a, **k):
        from ops_dashboard.brain import ProviderError
        self.calls += 1
        raise ProviderError("invalid_key", "bad key", 401)


def test_provider_error_cools_the_key_down_for_every_session(env):
    store = env["store"]
    a_prov, b_prov = Boom(), Boom()
    a = env["session"]("a", prov=a_prov)
    b = env["session"]("b", prov=b_prov)
    env["task"](a)
    env["task"](b)
    SessionRunner(store, a, provider_factory=env["factory"]).run_task(store.claim_task(a))
    assert store.get_session(a)["status"] == "waiting_human"
    SessionRunner(store, b, provider_factory=env["factory"]).run_task(store.claim_task(b))
    assert b_prov.calls == 0                                # same key (provider default): not hammered
    assert "provider.cooldown" in kinds(store, b)
    ev = [e for e in store.list_events(a) if e["kind"] == "provider.error"][0]
    assert json.loads(ev["payload"]) == {"code": "invalid_key", "cooldown_s": 600}


def test_adapter_explicit_provider_no_fallback_and_key_ref(monkeypatch):
    from ops_dashboard import brain, secrets_store
    p = providers.resolve_for_session({"provider": "mock", "model": "mock-x", "key_ref": None})
    assert p.name == "mock" and p.model == "mock-x"
    monkeypatch.setattr(brain, "_provider_is_in_cooldown", lambda n: n == "openai")
    with pytest.raises(providers.ProviderUnavailable) as ei:
        providers.resolve_for_session({"provider": "openai", "model": "m", "key_ref": None})
    assert ei.value.reason in ("cooldown", "provider_unavailable")
    monkeypatch.setattr(brain, "_provider_is_in_cooldown", lambda n: False)
    monkeypatch.setattr(brain, "resolve_provider", lambda *a, **k: brain.MockProvider())
    with pytest.raises(providers.ProviderUnavailable, match="could not be resolved"):
        providers.resolve_for_session({"provider": "openai", "model": "m", "key_ref": None})
    monkeypatch.setattr(brain, "resolve_provider",
                        lambda *a, **k: brain.OpenAIProvider(api_key="", model=None))
    monkeypatch.setattr(secrets_store, "get_secret",
                        lambda name: "fake-not-a-key" if name == "byok.openai.work" else None)
    before = dict(os.environ)
    p = providers.resolve_for_session({"provider": "openai", "model": "gpt-x",
                                       "key_ref": "byok.openai.work"})
    assert p.api_key == "fake-not-a-key" and p.model == "gpt-x"
    assert dict(os.environ) == before                       # never via env
    with pytest.raises(providers.ProviderUnavailable, match="no key"):
        providers.resolve_for_session({"provider": "openai", "model": "m",
                                       "key_ref": "byok.openai.missing"})
    with pytest.raises(providers.ProviderUnavailable, match="not supported"):
        providers.resolve_for_session({"provider": "ollama", "model": "m",
                                       "key_ref": "byok.ollama.x"})
    # A key stored for OpenAI is never read for (or sent to) another provider.
    read: list[str] = []
    monkeypatch.setattr(secrets_store, "get_secret", lambda name: read.append(name) or "k")
    for other in ("anthropic", "gateway"):
        with pytest.raises(providers.ProviderUnavailable, match="another provider"):
            providers.resolve_for_session({"provider": other, "model": "m",
                                           "key_ref": "byok.openai.work"})
    assert read == []


def test_session_shows_whether_its_total_includes_estimates(env):
    store = env["store"]
    real = Scripted([J("list_dir"), J(final="ok")],
                    usage={"input_tokens": 5, "output_tokens": 7, "source": "input_output"})
    a = env["session"]("a", prov=real)
    env["task"](a)
    SessionRunner(store, a, provider_factory=env["factory"]).run_task(store.claim_task(a))
    s = store.get_session(a)
    assert s["tokens_in"] == 10 and s["tokens_out"] == 14
    assert s["usage_includes_estimates"] is False
    est = Scripted([J(final="ok")])                       # provider reports no usage
    b = env["session"]("b", prov=est)
    env["task"](b)
    SessionRunner(store, b, provider_factory=env["factory"]).run_task(store.claim_task(b))
    assert store.get_session(b)["usage_includes_estimates"] is True
    ev = [e for e in store.list_events(b) if e["kind"] == "llm.turn"][0]
    assert json.loads(ev["payload"])["estimated"] is True
    assert json.loads(ev["payload"])["usage_source"] == "estimate"
