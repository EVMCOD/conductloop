"""Agent click/type with confirmations (browser design PR 9, §3.3, B10):
the effect classifier (shared vectors with Rust), approvals bound to the DOM
hash and single use, password refusal, the runner in ``act`` mode and the API.
No desktop app: the bridge is an in-process fake."""
from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest

from ops_dashboard.sessions import SessionStore, providers
from ops_dashboard.sessions import browser as sb
from ops_dashboard.sessions import browser_effects as fx
from ops_dashboard.sessions.browser_runner import BrowserRunner, system_prompt

ROOT = Path(__file__).resolve().parent.parent
VECTORS = ROOT / "desktop" / "url-policy" / "tests" / "effect_vectors.txt"
TAURI = ROOT / "desktop" / "src-tauri" / "src"


def J(tool=None, final=None, **args):
    return json.dumps({"final": final} if final is not None else {"tool": tool, "args": args})


class Scripted:
    def __init__(self, replies):
        self.name, self.model = "openai", "m"
        self.replies = list(replies)
        self.calls: list = []
        self.last_usage = None

    def ask(self, system, messages, **kw):
        self.calls.append((system, [dict(m) for m in messages]))
        return self.replies.pop(0) if self.replies else J(final="nothing left")


PAGE = {"url": "https://shop.example/p/1", "title": "Shop", "text": "A lamp. 10 EUR",
        "has_password": False, "has_payment": False,
        "elements": [
            {"ref": 1, "tag": "a", "name": "Details", "href": "https://shop.example/p/1/details"},
            {"ref": 2, "tag": "button", "name": "Add to cart", "in_form": True},
            {"ref": 3, "tag": "input", "type": "search", "name": "Search"},
            {"ref": 4, "tag": "input", "type": "password", "name": "Password", "in_form": True},
            {"ref": 5, "tag": "a", "name": "Elsewhere", "href": "https://other.example/"},
        ]}


class FakeBridge:
    available = True

    def __init__(self, snapshot=None, live_confirm: dict | None = None):
        self.snapshot = json.loads(json.dumps(snapshot or PAGE))
        self.tab = {"open": True, "id": "tab-1", "url": self.snapshot["url"], "control": "agent"}
        self.calls: list[tuple[str, dict]] = []
        self.live_confirm = live_confirm or {}   # ref → reason Rust would demand
        self.on_read = None

    def call(self, op, session, **args):
        self.calls.append((op, args))
        if op == "status":
            return {"ok": True, "tab": self.tab}
        if op in ("open", "navigate"):
            return {"ok": True, "tab": self.tab}
        if op == "read":
            if self.on_read:
                self.on_read(self)
            return {"ok": True, "snapshot": json.loads(json.dumps(self.snapshot))}
        if op in ("click", "type"):
            r = self.live_confirm.get(args["ref"])
            if r and not args.get("confirmed"):
                return {"ok": False, "error": f"confirmation_required:{r}"}
            return {"ok": True, "effect": "allow"}
        return {"ok": False, "error": "unknown_op"}

    def acts(self):
        return [c for c in self.calls if c[0] in ("click", "type")]


@pytest.fixture(autouse=True)
def _short_approvals(monkeypatch):
    # Nobody answers approvals in these tests unless a test says so.
    monkeypatch.setattr(sb, "APPROVAL_TIMEOUT_S", 1.0)
    monkeypatch.setattr(sb, "APPROVAL_POLL_S", 0.01)


@pytest.fixture(autouse=True)
def _reset():
    providers.reset_cooldowns()
    sb._reset_config_for_tests()
    yield
    providers.reset_cooldowns()
    sb._reset_config_for_tests()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    dbp = tmp_path / "a.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    st = SessionStore()
    st.set_model_price("openai", "m", in_cents_per_mtok=0, out_cents_per_mtok=1)
    return st


def session(store, mode="act"):
    return sb.create_browser_session(store, name="b", role="browser", provider="openai", model="m",
                                     budget_cost_cents=10_000, browser_mode=mode)


def tools_for(store, sid, bridge, **kw):
    t = sb.BrowserTools(store, sid, bridge=bridge, approval_timeout=kw.pop("timeout", 5.0), **kw)  # type: ignore[arg-type]
    assert t.read()["ok"]
    return t


def decide_when_pending(store, sid, decision, *, before=None):
    """Background 'user' that answers the first pending approval."""
    def go():
        for _ in range(500):
            rows = sb.pending_browser_approvals(store, sid)
            if rows:
                if before:
                    before()
                sb.resolve_browser_approval(store, rows[0]["id"], sid, decision)
                return
            threading.Event().wait(0.01)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(sb, "APPROVAL_POLL_S", 0.01)


# ── classifier: shared vectors with Rust ──────────────────────────────────────

def _vectors():
    for line in VECTORS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        lhs, expected = line.split("=>")
        f = [x.strip() for x in lhs.split("|")]
        assert len(f) == 12, line
        yield line, f, expected.strip()


def test_python_classifier_matches_shared_vectors():
    n = 0
    for line, f, expected in _vectors():
        action, submit = ("type", True) if f[0] == "type+submit" else (f[0], False)
        el = fx.Element(tag=f[1], typ=f[2], role=f[3], name=f[4], autocomplete=f[5],
                        in_form=f[6] == "1", href=None if f[7] in ("-", "") else f[7],
                        payment=f[8] == "1")
        page = fx.Page(host=f[9], has_password=f[10] == "1", has_payment=f[11] == "1")
        assert fx.classify(action, el, page, submit=submit) == expected, line
        n += 1
    assert n >= 20


def test_effect_verbs_and_sites():
    assert fx.has_effect_verb("Realizar PEDIDO") and fx.has_effect_verb("Suscríbete")
    assert fx.has_effect_verb("Place order") and not fx.has_effect_verb("Opportunities")
    assert fx.site("WWW.Example.com.") == "example.com"
    rs = (ROOT / "desktop" / "url-policy" / "src" / "effects.rs").read_text()
    for v in fx.EFFECT_VERBS:
        assert f'"{v}"' in rs, f"verb {v!r} missing in effects.rs"


# ── BrowserTools.click / type ─────────────────────────────────────────────────

def test_allowed_click_goes_straight_through(store):
    sid = session(store)
    br = FakeBridge()
    t = tools_for(store, sid, br)
    r = t.click(1)
    assert r["ok"] and br.acts() == [("click", {"ref": 1})]
    assert sb.pending_browser_approvals(store, sid) == []
    assert t.click(1)["error"] == "read the page first"   # must read after acting


def test_password_and_unknown_refs_are_refused(store):
    sid = session(store)
    br = FakeBridge()
    t = tools_for(store, sid, br)
    r = t.type(4, "hunter2")
    assert not r["ok"] and "password_field" in r["error"]
    assert "no element" in t.click(99)["error"] and "ref" in t.click("x")["error"]
    assert "text" in t.type(3, "x" * (sb.MAX_TYPE_CHARS + 1))["error"]
    assert br.acts() == []
    ev = [e for e in store.list_events(sid) if e["kind"] == "browser.denied"]
    assert ev and "hunter2" not in json.dumps(ev)


def test_read_mode_cannot_click(store):
    sid = session(store, "read")
    t = tools_for(store, sid, FakeBridge())
    with pytest.raises(sb.BrowserDenied):
        t.click(1)


def test_effectful_click_waits_for_approval_and_is_single_use(store):
    sid = session(store)
    br = FakeBridge()
    t = tools_for(store, sid, br)
    decide_when_pending(store, sid, "approve")
    r = t.click(2)
    assert r["ok"], r
    assert br.acts() == [("click", {"ref": 2, "confirmed": True})]
    events = [e["kind"] for e in store.list_events(sid)]
    assert "browser.approval_pending" in events and "browser.click" in events
    from ops_dashboard import db
    conn = db.get_connection()
    row = conn.execute("SELECT id, details FROM agent_logs WHERE action='approval_proposed'").fetchone()
    conn.close()
    d = json.loads(row[1])
    p = d["approval_payload"]
    assert d["approval_action"] == "browser.click" and d["used_at"]
    assert p["session"] == sid and p["ref"] == 2 and p["url"] == PAGE["url"] and len(p["dom_hash"]) == 32
    assert p["effect"] == "confirm:submit" and p["element"]["name"] == "Add to cart"
    assert not sb.consume_browser_approval(store, row[0], sid)          # single use
    assert not sb.resolve_browser_approval(store, row[0], sid, "approve")  # not pending


def test_typing_with_submit_shows_text_and_needs_approval(store):
    sid = session(store)
    br = FakeBridge()
    t = tools_for(store, sid, br)
    assert t.type(3, "lamp")["ok"]                    # plain search field: allowed
    t.read()
    decide_when_pending(store, sid, "approve")
    assert t.type(3, "lamp", True)["ok"]
    assert br.acts()[-1] == ("type", {"ref": 3, "text": "lamp", "submit": True, "confirmed": True})


def test_rejected_or_expired_does_nothing(store):
    sid = session(store)
    br = FakeBridge()
    t = tools_for(store, sid, br)
    decide_when_pending(store, sid, "reject")
    assert t.click(2)["error"] == "the user rejected this action"
    t2 = tools_for(store, sid, br, timeout=0.05)
    assert t2.click(5)["error"] == "nobody approved it in time"   # cross-domain link
    assert br.acts() == [] and sb.pending_browser_approvals(store, sid) == []


def test_cancel_while_waiting(store):
    sid = session(store)
    cancel = threading.Event()
    t = tools_for(store, sid, FakeBridge(), cancel=cancel)
    threading.Timer(0.1, cancel.set).start()
    assert t.click(2)["error"] == "cancelled"
    assert sb.pending_browser_approvals(store, sid) == []


def test_page_change_after_approval_invalidates_it(store):
    sid = session(store)
    br = FakeBridge()
    t = tools_for(store, sid, br)

    def swap():  # the page swaps the button for another one while we wait
        br.snapshot["elements"][1] = {"ref": 2, "tag": "button", "name": "Buy now", "in_form": True}
    decide_when_pending(store, sid, "approve", before=swap)
    r = t.click(2)
    assert not r["ok"] and "changed after approval" in r["error"]
    assert br.acts() == []
    assert "browser.approval_invalidated" in [e["kind"] for e in store.list_events(sid)]


def test_page_changed_before_asking(store):
    sid = session(store)
    br = FakeBridge()
    t = tools_for(store, sid, br)
    br.snapshot["url"] = "https://shop.example/checkout"
    assert "read it again" in t.click(2)["error"]
    assert sb.pending_browser_approvals(store, sid) == []


def test_rust_disagreement_escalates_to_approval(store):
    """Snapshot says harmless; the live element (Rust) needs confirmation."""
    sid = session(store)
    br = FakeBridge(live_confirm={1: "effect_verb"})
    t = tools_for(store, sid, br)
    decide_when_pending(store, sid, "approve")
    assert t.click(1)["ok"]
    assert br.acts() == [("click", {"ref": 1}), ("click", {"ref": 1, "confirmed": True})]


def test_approvals_of_other_sessions_cannot_be_decided(store):
    a, b = session(store), session(store)
    aid = sb.add_browser_approval(store, a, {"op": "click", "ref": 2}, reason="x")
    assert not sb.resolve_browser_approval(store, aid, b, "approve")
    assert sb.resolve_browser_approval(store, aid, a, "approve")
    assert not sb.consume_browser_approval(store, aid, b)
    assert sb.consume_browser_approval(store, aid, a)
    # They also appear in the generic queue.
    from ops_dashboard import db
    assert db.list_pending_approvals() == []


# ── runner ────────────────────────────────────────────────────────────────────

def test_system_prompt_tools_by_mode():
    act, read = system_prompt("act", "n"), system_prompt("read", "n")
    assert '"ref": 12' in act and '"ref": 12' not in read
    assert "never type passwords" in act and "cannot click" in read


def test_runner_act_mode_click_with_approval(store):
    sid = session(store)
    br = FakeBridge()
    prov = Scripted([J("read"), J("click", ref=2), J("read"), J(final="added to cart")])
    tid = store.enqueue_task(title="t", prompt="add the lamp", assigned_session_id=sid)
    task = store.claim_task(sid)
    assert task and task["id"] == tid
    decide_when_pending(store, sid, "approve")
    out = BrowserRunner(store, sid, provider_factory=lambda s: prov, bridge=br).run_task(task)  # type: ignore[arg-type]
    assert out.task_status == "needs_review"
    assert br.acts() == [("click", {"ref": 2, "confirmed": True})]
    msgs = prov.calls[-1][1]
    assert any(m["content"].startswith("TOOL_RESULT click:") and '"ok": true' in m["content"] for m in msgs)


# ── API + UI ──────────────────────────────────────────────────────────────────

class _H:
    def __init__(self, body=None, method="POST"):
        raw = json.dumps(body or {}).encode()
        self.headers = {"Content-Length": str(len(raw)), "Host": "localhost:7777"}
        self.rfile = io.BytesIO(raw)
        self.command = method
        self.sent = None

    def _send(self, status, body, ctype="text/html", headers=None):
        self.sent = (status, json.loads(body))

    def _send_500(self, e, where):  # pragma: no cover
        raise e


def test_api_mode_and_approvals(store):
    from ops_dashboard import routes_browser, routes_sessions
    routes_sessions.configure()
    try:
        sid = session(store, "read")
        h = _H({"browser_mode": "act"})
        routes_browser.set_browser_mode(h, sid=str(sid))
        assert h.sent[0] == 200 and h.sent[1]["session"]["browser_mode"] == "act"
        aid = sb.add_browser_approval(store, sid, {"op": "click", "ref": 2, "url": "https://x/"},
                                      reason="click")
        h = _H(method="GET")
        routes_browser.list_browser_approvals(h, sid=str(sid))
        assert h.sent[0] == 200 and [a["id"] for a in h.sent[1]["approvals"]] == [aid]
        for body, code in (({"id": aid, "decision": "maybe"}, 400), ({"id": "1", "decision": "approve"}, 400),
                           ({"id": aid + 99, "decision": "approve"}, 409),
                           ({"id": aid, "decision": "reject"}, 200), ({"id": aid, "decision": "approve"}, 409)):
            h = _H(body)
            routes_browser.decide_browser_approval(h, sid=str(sid))
            assert h.sent[0] == code, (body, h.sent)
        assert sb.browser_approval_status(store, aid) == "rejected"
        h = _H({"id": aid, "decision": "approve"})
        h.headers["Sec-Fetch-Site"] = "cross-site"
        routes_browser.decide_browser_approval(h, sid=str(sid))
        assert h.sent[0] == 403
    finally:
        routes_sessions.configure()


def test_ui_and_page_scripts():
    html = (ROOT / "ops_dashboard" / "web" / "browser.html").read_text()
    for c in ('id="approvals"', '"/browser-approvals"', 'decide("approve")', 'decide("reject")',
              'id="ag-act"', '? "act" : "read"'):
        assert c in html, c
    ap = html[html.index("function pollApprovals"):]
    assert "innerHTML" not in ap.split("function api")[0]
    act = (TAURI / "agent_act.js").read_text()
    assert "__ARGS__" in act and 'getOwnPropertyDescriptor(proto, "value").set' in act
    assert "password_field" in act and "eval(" not in act and "Function(" not in act
    snap = (TAURI / "agent_snapshot.js").read_text()
    assert "item.ac =" in snap and 'item.href = String(el.href' in snap
    bridge = (TAURI / "agent_bridge.rs").read_text()
    assert '"click", "type"' in bridge and "confirmation_required:" in bridge and "denied:" in bridge
