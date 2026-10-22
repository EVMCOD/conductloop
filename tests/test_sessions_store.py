"""ops_dashboard.sessions — limits, atomic task claims under concurrency, path
claim conflicts, events/messages, and that key values are never stored.
Throwaway DB per test; no network, no keychain, no real keys."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from ops_dashboard.sessions import (
    PathConflictError,
    SessionLimitError,
    SessionStore,
    SessionValidationError,
)
from ops_dashboard.sessions import paths as P

FAKE_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE0123456789"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "s.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(path))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    clock = Clock()
    store = SessionStore(clock=clock)
    repo = store.mark_repo(str(tmp_path / "repo"), project_key="demo")
    return store, repo, clock, path


def _session(store, repo, **kw):
    args = dict(name="backend", role="backend", provider="openai", model="gpt-x",
                repo_path=repo, base_ref="main", budget_cost_cents=200)
    args.update(kw)
    return store.create_session(**args)


# ── limits ──────────────────────────────────────────────────────────────────

def test_defaults_three_sessions_and_sixty_iterations(env):
    store, repo, *_ = env
    ids = [_session(store, repo, name=f"s{i}") for i in range(3)]
    s = store.get_session(ids[0])
    assert s["max_iterations"] == 60 and s["budget_currency"] == "EUR"
    assert s["status"] == "created" and s["key_ref"] is None
    with pytest.raises(SessionLimitError):
        _session(store, repo, name="s4")
    store.set_session_status(ids[0], "closed")  # frees a slot
    _session(store, repo, name="s4")


def test_configurable_up_to_hard_max_eight(env):
    store, repo, *_ = env
    for i in range(8):
        _session(store, repo, name=f"s{i}", max_active=8)
    with pytest.raises(SessionLimitError):
        _session(store, repo, name="s9", max_active=8)
    for bad in (0, 9, 100):
        with pytest.raises(SessionValidationError):
            _session(store, repo, name="x", max_active=bad)


@pytest.mark.parametrize("kw,field", [
    ({"budget_cost_cents": None}, "budget_cost_cents"),
    ({"budget_cost_cents": 0}, "budget_cost_cents"),
    ({"budget_cost_cents": 1.5}, "budget_cost_cents"),
    ({"budget_cost_cents": True}, "budget_cost_cents"),
    ({"budget_currency": "USD"}, "budget_currency"),
    ({"budget_tokens": 0}, "budget_tokens"),
    ({"max_iterations": 61}, "max_iterations"),
    ({"max_iterations": 0}, "max_iterations"),
    ({"model": ""}, "model"),
    ({"provider": " "}, "provider"),
    ({"base_ref": ""}, "base_ref"),
])
def test_session_validation(env, kw, field):
    store, repo, *_ = env
    with pytest.raises(SessionValidationError) as ei:
        _session(store, repo, **kw)
    assert ei.value.field == field
    assert store.list_sessions() == []


def test_iteration_limit_is_configurable_without_migration(env, monkeypatch):
    store, repo, *_ = env
    with pytest.raises(SessionValidationError, match="between 1 and 60"):
        _session(store, repo, max_iterations=61)          # default limit = 60
    raised = SessionStore(clock=store._clock, max_iterations_limit=200)
    s = _session(raised, repo, name="long", max_iterations=200)
    assert raised.get_session(s)["max_iterations"] == 200
    with pytest.raises(SessionValidationError):
        _session(raised, repo, name="too-long", max_iterations=201)
    monkeypatch.setenv("CONDUCTLOOP_SESSIONS_MAX_ITERATIONS", "120")
    assert store.get_session(_session(store, repo, name="env", max_iterations=120))["max_iterations"] == 120
    monkeypatch.setenv("CONDUCTLOOP_SESSIONS_MAX_ITERATIONS", "abc")
    with pytest.raises(SessionValidationError):
        _session(store, repo, name="bad-env")
    for bad in (0, -1, True):
        with pytest.raises(SessionValidationError):
            SessionStore(max_iterations_limit=bad).create_session(
                name="x", role="r", provider="p", model="m", repo_path=repo, base_ref="main",
                budget_cost_cents=1)


def test_only_marked_repos(env, tmp_path):
    store, repo, *_ = env
    with pytest.raises(SessionValidationError, match="not marked"):
        _session(store, str(tmp_path / "unmarked"))
    store.mark_repo(repo, enabled=False)
    with pytest.raises(SessionValidationError, match="not marked"):
        _session(store, repo)
    store.mark_repo(repo, enabled=True)
    assert _session(store, repo)


def test_session_limit_is_atomic_under_concurrency(env):
    store, repo, *_ = env
    barrier, ok, errs = threading.Barrier(10), [], []

    def worker(i):
        barrier.wait()
        try:
            ok.append(_session(store, repo, name=f"c{i}"))
        except SessionLimitError:
            errs.append(i)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len(ok) == 3 and len(errs) == 7
    assert len(store.list_sessions(active_only=True)) == 3


# ── tasks ───────────────────────────────────────────────────────────────────

def test_user_assigns_and_only_that_session_claims(env):
    store, repo, *_ = env
    a, b = _session(store, repo, name="a"), _session(store, repo, name="b")
    backlog = store.enqueue_task(title="unassigned", prompt="p")
    t = store.enqueue_task(title="for a", prompt="p", assigned_session_id=a)
    assert store.claim_task(b) is None and store.claim_task(b, t) is None
    assert store.claim_task(a, backlog) is None  # D8: unassigned isn't claimable
    got = store.claim_task(a)
    assert got["id"] == t and got["owner_session_id"] == a and got["status"] == "claimed"
    assert store.claim_task(a, t) is None  # already owned
    assert store.assign_task(backlog, b) and store.claim_task(b)["id"] == backlog


def test_claim_order_priority_deps_and_one_in_flight(env):
    store, repo, *_ = env
    a = _session(store, repo)
    low = store.enqueue_task(title="low", prompt="p", assigned_session_id=a)
    dep = store.enqueue_task(title="dep", prompt="p", assigned_session_id=a, priority=9,
                             depends_on=[low])
    hi = store.enqueue_task(title="hi", prompt="p", assigned_session_id=a, priority=5)
    assert store.claim_task(a)["id"] == hi          # dep blocked by unfinished `low`
    assert store.claim_task(a) is None              # one in-flight task per session
    assert store.set_task_status(hi, a, "done")
    assert store.claim_task(a)["id"] == low
    assert store.set_task_status(low, a, "done")
    assert store.claim_task(a)["id"] == dep


def test_paused_or_closed_session_cannot_claim(env):
    store, repo, *_ = env
    a = _session(store, repo)
    store.enqueue_task(title="t", prompt="p", assigned_session_id=a)
    store.set_session_status(a, "paused")
    assert store.claim_task(a) is None
    store.set_session_status(a, "idle")
    assert store.claim_task(a) is not None


def test_requeue_clears_owner_and_counts_attempts(env):
    store, repo, *_ = env
    a = _session(store, repo)
    t = store.enqueue_task(title="t", prompt="p", assigned_session_id=a)
    store.claim_task(a)
    assert not store.set_task_status(t, a + 999, "done")  # not the owner
    assert store.set_task_status(t, a, "failed")
    row = store.get_task(t)
    assert row["owner_session_id"] is None and row["attempts"] == 1
    with pytest.raises(SessionValidationError):
        store.set_task_status(t, a, "claimed")  # only claim_task may claim


def test_atomic_claim_many_threads_one_task(env):
    """20 threads of the same session race for 5 queued tasks: exactly one wins
    (single in-flight task), and no task ever gets two owners."""
    store, repo, *_ = env
    a = _session(store, repo)
    for i in range(5):
        store.enqueue_task(title=f"t{i}", prompt="p", assigned_session_id=a)
    barrier, wins = threading.Barrier(20), []

    def worker():
        barrier.wait()
        r = store.claim_task(a)
        if r:
            wins.append(r["id"])

    ts = [threading.Thread(target=worker) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len(wins) == 1


def test_atomic_claim_stress_every_task_exactly_once(env):
    """8 sessions × 4 assigned tasks; each session runs 3 competing workers that
    loop claim → done. Every task is claimed exactly once, by its assignee."""
    store, repo, *_ = env
    sessions = [_session(store, repo, name=f"s{i}", max_active=8) for i in range(8)]
    expected = {}
    for s in sessions:
        for j in range(4):
            expected[store.enqueue_task(title=f"{s}-{j}", prompt="p", assigned_session_id=s)] = s
    claims, lock = [], threading.Lock()
    barrier = threading.Barrier(24)

    def worker(sid):
        barrier.wait()
        idle = 0
        while idle < 50:
            r = store.claim_task(sid)
            if r is None:
                idle += 1
                continue
            idle = 0
            with lock:
                claims.append((r["id"], sid))
            assert store.set_task_status(r["id"], sid, "done")

    ts = [threading.Thread(target=worker, args=(s,)) for s in sessions for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    ids = [c[0] for c in claims]
    assert sorted(ids) == sorted(expected)            # all claimed, none twice
    assert all(expected[tid] == sid for tid, sid in claims)
    assert all(store.get_task(t)["attempts"] == 1 for t in expected)


# ── path claims ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b,expected", [
    ("src/app", "src/app/x.py", True),       # directory covers file
    ("src/app", "src/apple.py", False),      # path boundary
    ("src/a*", "src/apple.py", True),        # glob literal prefix
    ("src/*.py", "src/x/y.py", True),        # conservative
    ("docs/**", "src/a.py", False),          # diverging prefixes
    ("src/a*", "src/b*", False),
    ("**", "anything/at/all", True),
    ("./src//app/", "src/app", True),        # normalisation
])
def test_overlap_rules(a, b, expected):
    assert P.overlaps(a, b) is expected
    assert P.overlaps(b, a) is expected


@pytest.mark.parametrize("pattern,exclusive", [
    ("ops_dashboard/server.py", True), ("ops_dashboard/web/index.html", True),
    ("ops_dashboard/db.py", True), ("package-lock.json", True), ("web/uv.lock", True),
    ("ops_dashboard/web/*.html", True), ("**", True),
    ("ops_dashboard/db_tasks.py", False), ("docs/readme.md", False), ("ops_dashboard", False),
])
def test_hot_files_and_lockfiles_are_exclusive(pattern, exclusive):
    assert P.is_exclusive(pattern) is exclusive


def test_bad_patterns_rejected():
    for bad in ("", "/etc/passwd", "../x", "a/../../b", "C:/x"):
        with pytest.raises(ValueError):
            P.normalize(bad)


def test_cross_session_overlap_conflicts_and_grants_nothing(env):
    store, repo, *_ = env
    a, b = _session(store, repo, name="a"), _session(store, repo, name="b")
    (c,) = store.claim_paths(a, ["src/app"])
    assert c["mode"] == "write"
    with pytest.raises(PathConflictError) as ei:
        store.claim_paths(b, ["docs/x.md", "src/app/x.py"])  # all-or-nothing
    conf = ei.value.conflicts[0]
    assert conf["held_session_id"] == a and conf["held_pattern"] == "src/app"
    assert store.active_claims(session_id=b) == []
    assert store.claim_paths(b, ["src/apple.py", "docs/x.md"])
    with pytest.raises(PathConflictError):
        store.claim_paths(b, ["src/a*"])


def test_normal_claims_shareable_within_session_exclusive_not(env):
    store, repo, *_ = env
    a, b = _session(store, repo, name="a"), _session(store, repo, name="b")
    t1 = store.enqueue_task(title="t1", prompt="p", assigned_session_id=a)
    t2 = store.enqueue_task(title="t2", prompt="p", assigned_session_id=a)
    store.claim_paths(a, ["ops_dashboard/sessions"], task_id=t1)
    store.claim_paths(a, ["ops_dashboard/sessions/store.py"], task_id=t2)  # same session: ok
    (hot,) = store.claim_paths(a, ["ops_dashboard/server.py"], task_id=t1)
    assert hot["mode"] == "exclusive"
    with pytest.raises(PathConflictError):  # exclusive: one task at a time, even same session
        store.claim_paths(a, ["ops_dashboard/server.py"], task_id=t2)
    with pytest.raises(PathConflictError):  # directory covering an exclusive file
        store.claim_paths(a, ["ops_dashboard"], task_id=t2)
    with pytest.raises(PathConflictError):  # other session, glob hitting it
        store.claim_paths(b, ["ops_dashboard/*.py"])
    # Re-claiming the same pattern for the same task is idempotent.
    assert store.claim_paths(a, ["ops_dashboard/server.py"], task_id=t1)[0]["id"] == hot["id"]


def test_lockfile_exclusive_between_tasks(env):
    store, repo, *_ = env
    a = _session(store, repo)
    t1 = store.enqueue_task(title="t1", prompt="p", assigned_session_id=a)
    t2 = store.enqueue_task(title="t2", prompt="p", assigned_session_id=a)
    store.claim_paths(a, ["web/package-lock.json"], task_id=t1)
    with pytest.raises(PathConflictError):
        store.claim_paths(a, ["web/package-lock.json"], task_id=t2)


def test_release_expiry_and_lifecycle(env):
    store, repo, clock, _ = env
    a, b = _session(store, repo, name="a"), _session(store, repo, name="b")
    t = store.enqueue_task(title="t", prompt="p", assigned_session_id=a)
    store.claim_task(a)
    store.claim_paths(a, ["src"], task_id=t)
    assert store.can_write(a, "src/x.py") and not store.can_write(b, "src/x.py")
    assert not store.can_write(a, "other/x.py")  # D3: no claim, no write
    assert store.set_task_status(t, a, "done")   # done releases the task's claims
    assert not store.can_write(a, "src/x.py")
    store.claim_paths(b, ["src"])                 # now free
    store.release_paths(b)
    store.claim_paths(a, ["lib"], ttl_seconds=60)
    clock.t += 61                                 # expired claims don't block
    assert store.claim_paths(b, ["lib/z.py"])
    store.set_session_status(b, "closed")         # terminal session releases all
    assert store.active_claims(session_id=b) == []
    with pytest.raises(SessionValidationError):
        store.claim_paths(b, ["x"])               # closed sessions can't claim


def test_concurrent_claims_same_path_one_winner(env):
    store, repo, *_ = env
    sessions = [_session(store, repo, name=f"s{i}", max_active=8) for i in range(8)]
    barrier, wins, losses = threading.Barrier(8), [], []

    def worker(sid):
        barrier.wait()
        try:
            store.claim_paths(sid, ["ops_dashboard/db.py"])
            wins.append(sid)
        except PathConflictError:
            losses.append(sid)

    ts = [threading.Thread(target=worker, args=(s,)) for s in sessions]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len(wins) == 1 and len(losses) == 7
    assert len(store.active_claims()) == 1


# ── events, messages, prices ────────────────────────────────────────────────

def test_events_accumulate_usage_with_user_prices_only(env):
    store, repo, *_ = env
    a = _session(store, repo, budget_cost_cents=100)
    assert store.list_model_prices() == []        # nothing invented
    r = store.record_event(a, "llm_call", {"x": 1}, tokens_in=1000, tokens_out=500, iteration=True)
    assert r["cost_cents_est"] == 0 and r["price_missing"] is True
    store.set_model_price("openai", "gpt-x", in_cents_per_mtok=1000, out_cents_per_mtok=4000)
    r = store.record_event(a, "llm_call", tokens_in=1_000_000, tokens_out=10_000, iteration=True)
    assert r["cost_cents_est"] == pytest.approx(1040) and r["price_missing"] is False
    s = store.get_session(a)
    assert s["tokens_in"] == 1_001_000 and s["iterations"] == 2
    assert r["budget_ratio"] == pytest.approx(10.4) and r["iterations_left"] == 58
    assert [e["kind"] for e in store.list_events(a)] == ["llm_call", "llm_call"]
    with pytest.raises(SessionValidationError):
        store.set_model_price("openai", "gpt-x", in_cents_per_mtok=-1, out_cents_per_mtok=0)


def test_ollama_has_no_missing_price(env):
    store, repo, *_ = env
    a = _session(store, repo, provider="ollama", model="llama3")
    assert store.record_event(a, "llm_call", tokens_in=10)["price_missing"] is False


def test_messages(env):
    store, repo, *_ = env
    a, b = _session(store, repo, name="a"), _session(store, repo, name="b")
    m = store.post_message(kind="conflict", body="db.py is mine", from_session=a, to_session=b)
    store.post_message(kind="info", body="to the human", from_session=a)
    assert [x["id"] for x in store.list_messages(b, unread_only=True)] == [m]
    assert store.mark_read([m]) == 1 and store.list_messages(b, unread_only=True) == []
    assert len(store.list_messages(None)) == 1
    with pytest.raises(SessionValidationError):
        store.post_message(kind="shout", body="x")


# ── secrets ─────────────────────────────────────────────────────────────────

def test_key_ref_only_never_a_key_value(env):
    store, repo, *_ = env
    s = _session(store, repo, key_ref="byok.anthropic.default", provider="anthropic")
    assert store.get_session(s)["key_ref"] == "byok.anthropic.default"
    for bad in (FAKE_KEY, "byok." + FAKE_KEY, "byok.anthropic." + FAKE_KEY, "anthropic", "byok.."):
        with pytest.raises(SessionValidationError) as ei:
            _session(store, repo, name="x", key_ref=bad)
        assert FAKE_KEY not in str(ei.value)


def test_key_values_never_persisted_anywhere(env):
    store, repo, _clock, path = env
    a = _session(store, repo, key_ref="byok.openai.default")
    t = store.enqueue_task(title="t", prompt="p", assigned_session_id=a)
    store.claim_task(a)
    store.record_event(a, "tool_call", {"env": {"OPENAI_API_KEY": FAKE_KEY}, "out": [f"k={FAKE_KEY}"]})
    store.post_message(kind="info", body=f"my key is {FAKE_KEY}", from_session=a)
    store.set_session_status(a, "failed", last_error=f"401 for key {FAKE_KEY}")
    store.set_task_status(t, a, "done", result_summary=f"used {FAKE_KEY}")
    conn = sqlite3.connect(path)
    rows = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        try:
            rows.extend(map(repr, conn.execute(f'SELECT * FROM "{name}"').fetchall()))
        except sqlite3.OperationalError:  # main's unscannable FTS table; not ours
            continue
    dump = "\n".join(rows)
    assert FAKE_KEY not in dump
    assert "[redacted-secret]" in dump
    cols = {r[1] for t_ in ("agent_sessions", "session_tasks", "path_claims", "session_messages",
                            "session_events", "model_prices", "session_repos")
            for r in conn.execute(f"PRAGMA table_info({t_})")}
    assert not {c for c in cols if "api_key" in c or c in ("secret", "password", "token")}


# ── review fixes (Codex on #18/#19) ─────────────────────────────────────────

def test_terminal_session_requeues_its_work_and_never_comes_back(env):
    store, repo, *_ = env
    a = _session(store, repo, name="a")
    t = store.enqueue_task(title="t", prompt="p", assigned_session_id=a)
    assert store.claim_task(a)["id"] == t
    store.set_session_status(a, "failed", last_error="crash")
    task = store.get_task(t)
    assert task["status"] == "queued" and task["owner_session_id"] is None
    b = _session(store, repo, name="b")
    assert store.assign_task(t, b) and store.claim_task(b)["id"] == t      # recoverable
    with pytest.raises(SessionValidationError):
        store.set_session_status(a, "idle")                                 # no slot-limit bypass
    assert store.get_session(a)["status"] == "failed"


def test_claims_cannot_borrow_another_sessions_task(env):
    store, repo, *_ = env
    a, b = _session(store, repo, name="a"), _session(store, repo, name="b")
    tb = store.enqueue_task(title="tb", prompt="p", assigned_session_id=b)
    with pytest.raises(SessionValidationError):
        store.claim_paths(a, ["src/a.py"], task_id=tb)
    assert store.active_claims(session_id=a) == []


def test_unmarking_a_repo_revokes_writes(env):
    store, repo, *_ = env
    a = _session(store, repo, name="a")
    store.claim_paths(a, ["src"])
    assert store.can_write(a, "src/x.py")
    store.mark_repo(repo, enabled=False)
    assert not store.can_write(a, "src/x.py")
    with pytest.raises(SessionValidationError):
        store.claim_paths(a, ["src"])


def test_hot_files_need_their_own_exclusive_claim(env):
    store, repo, *_ = env
    a = _session(store, repo, name="a")
    store.claim_paths(a, ["ops_dashboard"])
    assert store.can_write(a, "ops_dashboard/routes.py")
    assert not store.can_write(a, "ops_dashboard/server.py")     # a dir claim is shareable
    store.claim_paths(a, ["ops_dashboard/server.py"])
    assert store.can_write(a, "ops_dashboard/server.py")


def test_unknown_dependencies_are_rejected_and_block(env):
    store, repo, *_ = env
    a = _session(store, repo, name="a")
    with pytest.raises(SessionValidationError):
        store.enqueue_task(title="t", prompt="p", assigned_session_id=a, depends_on=[999])
    dep = store.enqueue_task(title="dep", prompt="p")
    t = store.enqueue_task(title="t", prompt="p", assigned_session_id=a, depends_on=[dep])
    with store._tx() as c:                                       # the dependency disappears
        c.execute("DELETE FROM session_tasks WHERE id = ?", (dep,))
    assert store.claim_task(a, t) is None
