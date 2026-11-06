"""Agent browser tools v2: ``back``, ``scroll`` and ``select``.

Same gates as the first tools: ``back``/``scroll`` belong to ``read`` mode,
``select`` to ``act`` mode and goes through the shared effect classifier and
the approval queue (payment selects, sensitive pages). No desktop app: the
bridge is an in-process fake, and the Rust/JS side is checked from source.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from ops_dashboard.sessions import SessionStore, providers
from ops_dashboard.sessions import browser as sb
from ops_dashboard.sessions import browser_effects as fx
from ops_dashboard.sessions.browser_runner import BrowserRunner, system_prompt

ROOT = Path(__file__).resolve().parent.parent
TAURI = ROOT / "desktop" / "src-tauri" / "src"

PAGE = {"url": "https://shop.example/p/1", "title": "Shop", "text": "A lamp. 10 EUR",
        "has_password": False, "has_payment": False,
        "elements": [
            {"ref": 1, "tag": "a", "name": "Details", "href": "https://shop.example/p/1/details"},
            {"ref": 2, "tag": "select", "name": "Size", "in_form": True,
             "options": ["Small", "Medium", "Large"], "selected": "Small", "filled": True},
            {"ref": 3, "tag": "input", "type": "search", "name": "Search"},
            {"ref": 4, "tag": "select", "name": "Expiry month", "in_form": True,
             "payment": True, "ac": "cc-exp-month"},
        ]}


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


class FakeBridge:
    available = True

    def __init__(self, snapshot=None, *, y=0, top=2000):
        self.snapshot = json.loads(json.dumps(snapshot or PAGE))
        self.tab = {"open": True, "id": "tab-1", "url": self.snapshot["url"], "control": "agent"}
        self.calls: list[tuple[str, dict]] = []
        self.y, self.top = y, top

    def call(self, op, session, **args):
        self.calls.append((op, args))
        if op == "status":
            return {"ok": True, "tab": self.tab}
        if op in ("open", "navigate", "back"):
            return {"ok": True, "tab": self.tab}
        if op == "read":
            return {"ok": True, "snapshot": json.loads(json.dumps(self.snapshot))}
        if op == "scroll":
            self.y = max(0, min(self.top, self.y + (800 if args["direction"] == "down" else -800)))
            return {"ok": True, "y": self.y, "max": self.top, "tab": self.tab}
        if op in ("click", "type", "select"):
            return {"ok": True, "effect": "allow"}
        return {"ok": False, "error": "unknown_op"}

    def ops(self, *names):
        return [c for c in self.calls if c[0] in names]


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(sb, "APPROVAL_TIMEOUT_S", 1.0)
    monkeypatch.setattr(sb, "APPROVAL_POLL_S", 0.01)
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


def answer_first_approval(store, sid, decision):
    def go():
        for _ in range(500):
            rows = sb.pending_browser_approvals(store, sid)
            if rows:
                sb.resolve_browser_approval(store, rows[0]["id"], sid, decision)
                return
            threading.Event().wait(0.01)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


# ── modes ────────────────────────────────────────────────────────────────────

def test_back_and_scroll_are_read_tools_select_is_an_act_tool():
    assert {"back", "scroll"} <= set(sb.allowed_tools("read"))
    assert "select" not in sb.allowed_tools("read")
    assert {"back", "scroll", "select"} <= set(sb.allowed_tools("act"))
    assert sb.allowed_tools("off") == ()


def test_read_mode_cannot_select_and_off_mode_cannot_go_back(store):
    sid = session(store, mode="read")
    br = FakeBridge()
    t = tools_for(store, sid, br)
    with pytest.raises(sb.BrowserDenied):
        t.select(2, "Large")
    assert t.back()["ok"] and t.scroll("down")["ok"]
    t.mode = lambda: "off"            # a session that is no longer a browser session
    with pytest.raises(sb.BrowserDenied):
        t.back()
    with pytest.raises(sb.BrowserDenied):
        t.scroll()
    assert br.ops("select") == [] and len(br.ops("back")) == 1
    denied = [e for e in store.list_events(sid) if e["kind"] == "browser.denied"]
    assert len(denied) == 3             # select (read mode), back and scroll (off)


# ── back ─────────────────────────────────────────────────────────────────────

def test_back_asks_the_bridge_and_forces_a_new_read(store):
    sid = session(store)
    br = FakeBridge()
    t = tools_for(store, sid, br)
    r = t.back()
    assert r["ok"] and "read the page" in r["note"]
    assert br.ops("back") == [("back", {})]
    assert t.click(1)["error"] == "read the page first"
    assert any(e["kind"] == "browser.back" for e in store.list_events(sid))


def test_back_reports_the_bridge_refusal(store):
    sid = session(store)
    br = FakeBridge()
    t = tools_for(store, sid, br)
    br.call = lambda op, s, **a: ({"ok": False, "error": "not_in_control"} if op == "back"
                                  else FakeBridge.call(br, op, s, **a))
    assert t.back() == {"ok": False, "error": "not_in_control"}


# ── scroll ───────────────────────────────────────────────────────────────────

def test_scroll_validates_the_direction_and_reports_the_end(store):
    sid = session(store)
    br = FakeBridge(top=1600)
    t = tools_for(store, sid, br)
    assert "direction" in t.scroll("sideways")["error"]
    assert "direction" in t.scroll("0);alert(1")["error"]
    assert br.ops("scroll") == []
    r1, r2 = t.scroll("down"), t.scroll("DOWN")
    assert (r1["y"], r1["at_end"]) == (800, False) and (r2["y"], r2["at_end"]) == (1600, True)
    assert t.scroll("up")["y"] == 800
    assert [a["direction"] for _, a in br.ops("scroll")] == ["down", "down", "up"]
    # Scrolling does not change the refs: the agent may act without reading again.
    assert t.click(1)["ok"]


# ── select ───────────────────────────────────────────────────────────────────

def test_select_goes_through_when_allowed(store):
    sid = session(store)
    br = FakeBridge()
    t = tools_for(store, sid, br)
    r = t.select(2, " Large ")
    assert r["ok"] and br.ops("select") == [("select", {"ref": 2, "text": "Large"})]
    assert sb.pending_browser_approvals(store, sid) == []
    assert t.select(2, "Medium")["error"] == "read the page first"


def test_select_refuses_bad_options_and_non_selects(store):
    sid = session(store)
    br = FakeBridge()
    t = tools_for(store, sid, br)
    for bad in ("", "   ", None, 3, "x" * (sb.MAX_OPTION_CHARS + 1)):
        assert "option" in t.select(2, bad)["error"]
    r = t.select(3, "Large")
    assert not r["ok"] and "not_a_select" in r["error"]
    assert "no element" in t.select(99, "Large")["error"]
    assert br.ops("select") == []


def test_a_payment_select_waits_for_the_user(store):
    sid = session(store)
    br = FakeBridge()
    t = tools_for(store, sid, br)
    answer_first_approval(store, sid, "approve")
    r = t.select(4, "07")
    assert r["ok"]
    assert br.ops("select") == [("select", {"ref": 4, "text": "07", "confirmed": True})]
    ev = [e for e in store.list_events(sid) if e["kind"] == "browser.approval_pending"]
    assert ev and "payment_field" in json.dumps(ev)


def test_a_rejected_select_does_nothing(store):
    sid = session(store)
    br = FakeBridge()
    t = tools_for(store, sid, br)
    answer_first_approval(store, sid, "reject")
    assert t.select(4, "07")["error"] == "the user rejected this action"
    assert br.ops("select") == []


def test_classifier_select_rules():
    page = fx.Page(host="shop.example", has_password=False, has_payment=False)
    sel = fx.Element(tag="select", typ="", role="", name="Size", autocomplete="", in_form=True,
                     href=None, payment=False)
    assert fx.classify("select", sel, page) == "allow"
    assert fx.classify("select", sel, fx.Page(host="shop.example", has_password=True,
                                              has_payment=False)) == "confirm:sensitive_page"
    inp = fx.Element(tag="input", typ="text", role="", name="Size", autocomplete="",
                     in_form=True, href=None, payment=False)
    assert fx.classify("select", inp, page) == "deny:not_a_select"


# ── what the model sees ──────────────────────────────────────────────────────

def test_the_read_lists_the_options_of_a_select():
    text, _ = sb.format_snapshot(PAGE)
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("[2]"))
    assert 'options=["Small", "Medium", "Large"]' in line and 'selected="Small"' in line
    assert "options=" not in next(ln for ln in text.splitlines() if ln.strip().startswith("[4]"))


def test_the_prompt_offers_the_tools_of_the_mode():
    read, act = system_prompt("read", "n0nce"), system_prompt("act", "n0nce")
    assert "  back " in read and "  scroll " in read and "  select " not in read
    assert "  select " in act and "  back " in act


def test_runner_uses_scroll_select_and_back(store):
    sid = session(store)
    br = FakeBridge()
    prov = Scripted([J("read"), J("scroll", direction="down"), J("select", ref=2, option="Large"),
                     J("back"), J(final="chose Large")])
    store.enqueue_task(title="t", prompt="pick size Large on shop.example", assigned_session_id=sid)
    task = store.claim_task(sid)
    out = BrowserRunner(store, sid, provider_factory=lambda s: prov, bridge=br).run_task(task)  # type: ignore[arg-type]
    assert out.task_status == "needs_review"
    assert [c[0] for c in br.calls if c[0] in ("scroll", "select", "back")] == ["scroll", "select", "back"]
    msgs = prov.calls[-1][1]
    for name in ("scroll", "select", "back"):
        assert any(m["content"].startswith(f"TOOL_RESULT {name}:") and '"ok": true' in m["content"]
                   for m in msgs), name


# ── the desktop side (source checks; the Rust unit tests run in CI) ──────────

def test_the_bridge_knows_the_new_ops():
    rs = (TAURI / "agent_bridge.rs").read_text()
    for op in ('"back"', '"scroll"', '"select"'):
        assert op in rs.split("pub const OPS")[1].split("];")[0]
    assert "MAX_OPTION_CHARS: usize = 200" in rs and sb.MAX_OPTION_CHARS == 200
    act = (TAURI / "agent_act.js").read_text()
    assert 'args.op === "select"' in act and "HTMLSelectElement.prototype" in act
    assert "eval(" not in act and "querySelector('[data-cl-ref=\"' + String(parseInt(" in act
    snap = (TAURI / "agent_snapshot.js").read_text()
    assert "item.options" in snap and 'indexOf("cc-") !== 0' in snap


# ── the page scripts in a real headless Chrome (skipped without Chrome) ─────

_CHROME = next((p for p in (os.environ.get("CHROME_BIN"), shutil.which("google-chrome"),
                            shutil.which("chromium"), shutil.which("chromium-browser")) if p), None)

_SELECT_PAGE = """
<form><label for=s>Size</label>
<select id=s name=size><option>Small</option><option value=m>Medium</option>
<option disabled>Huge</option><option>Large</option></select>
<select id=cc autocomplete=cc-exp-month><option>01</option><option>02</option></select>
<input id=q type=search name=q></form>
<script>window.changes = []; document.getElementById('s').addEventListener('change',
  function (e) { window.changes.push(e.target.value); });</script>
"""


def _chrome_run(tmp_path: Path, body_js: str) -> dict:
    def src(name: str) -> str:
        return (TAURI / name).read_text(encoding="utf-8").replace(
            "/*__CL_VISIBILITY__*/", (TAURI / "visibility.js").read_text(encoding="utf-8"), 1)

    act = src("agent_act.js").rsplit("(__ARGS__)", 1)[0]       # the function, args added below
    runner = ("<script>window.addEventListener('load', function () { var r; try {"
              "var SNAP = function () { return (\n" + src("agent_snapshot.js") + "\n); };"
              "var ACT = (\n" + act + "\n);" + body_js +
              "} catch (e) { r = {error: String(e)}; }"
              "var o = document.createElement('textarea'); o.id = 'cl-out';"
              "o.textContent = JSON.stringify(r); document.documentElement.appendChild(o); });</script>")
    page = tmp_path / "select.html"
    page.write_text(f"<!doctype html><html><head><title>S</title></head><body>{_SELECT_PAGE}{runner}"
                    "</body></html>", encoding="utf-8")
    out = subprocess.run(
        [str(_CHROME), "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
         "--disable-extensions", "--disable-background-networking", "--window-size=1280,900",
         f"--user-data-dir={tmp_path / 'profile'}", "--virtual-time-budget=3000", "--dump-dom",
         page.as_uri()], capture_output=True, text=True, timeout=90, check=False)
    m = re.search(r'<textarea id="cl-out">(.*?)</textarea>', out.stdout, re.S)
    assert m, out.stderr[-2000:]
    res = json.loads(html.unescape(m.group(1)))
    assert "error" not in res, res
    return res


@pytest.mark.skipif(_CHROME is None, reason="no headless Chrome/Chromium")
def test_select_and_options_in_a_real_page(tmp_path):
    res = _chrome_run(tmp_path, """
      var snap = SNAP();
      var size = snap.elements.filter(function (e) { return e.name === 'Size'; })[0];
      var cc = snap.elements.filter(function (e) { return e.ac === 'cc-exp-month'; })[0];
      var q = snap.elements.filter(function (e) { return e.type === 'search'; })[0];
      r = {size: size, cc: cc,
           big: ACT({op: 'select', ref: size.ref, text: 'Large'}),
           byValue: ACT({op: 'select', ref: size.ref, text: 'm'}),
           disabled: ACT({op: 'select', ref: size.ref, text: 'Huge'}),
           missing: ACT({op: 'select', ref: size.ref, text: 'XL'}),
           notSelect: ACT({op: 'select', ref: q.ref, text: 'Large'}),
           describe: ACT({op: 'describe', ref: size.ref}),
           value: document.getElementById('s').value, changes: window.changes};
    """)
    assert res["size"]["options"] == ["Small", "Medium", "Large"]
    assert res["size"]["selected"] == "Small"
    assert "options" not in res["cc"] and "selected" not in res["cc"]
    assert res["big"] == {"ok": True, "chosen": "Large"}
    assert res["byValue"] == {"ok": True, "chosen": "Medium"}
    assert res["disabled"]["error"] == "disabled_option"
    assert res["missing"]["error"] == "no_such_option"
    assert res["notSelect"]["error"] == "not_a_select"
    assert res["describe"]["tag"] == "select" and res["describe"]["name"] == "Size"
    assert res["value"] == "m" and res["changes"] == ["Large", "m"]
