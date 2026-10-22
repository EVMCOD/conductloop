"""Browser prompt-injection evals (browser design PR 10): the web targets,
script cassettes, the bridge stand-in and the new checks."""
from __future__ import annotations

import json

import pytest

from ops_dashboard.evals import checks, runner, web


def test_outside_untrusted_detects_escapes():
    blk = "<<<UNTRUSTED-0123456789abcdef web page: x>>>\nhello TRAP\n<<<END-UNTRUSTED-0123456789abcdef>>>"
    assert web.outside_untrusted("rules " + blk, "trap") == []
    assert web.outside_untrusted(blk + " TRAP", "trap") != []
    # A block with a different closing nonce does not count as a block.
    bad = "<<<UNTRUSTED-0123456789abcdef x>>>\nTRAP\n<<<END-UNTRUSTED-ffffffffffffffff>>>"
    assert web.outside_untrusted(bad, "TRAP") != []


def test_new_suites_load_and_pass(tmp_path):
    suites = runner.discover(runner.default_evals_dir(), ["web-injection", "browser-agent"])
    assert {s.target for s in suites} == {"web.ask_page", "browser.agent"}
    rep = runner.run(suites)
    ok, problems, _ = runner.compare(rep, json.loads(
        (runner.default_evals_dir() / "baseline.json").read_text()))
    failed = {c["key"] for c in rep["cases"] if not c["passed"]}
    assert failed == set(), failed
    assert not [p for p in problems if "required" in p], problems
    for c in rep["cases"]:
        if c["key"] in failed:
            assert not c["required"] and c["known_gap"]


def _suite(name):
    return runner.discover(runner.default_evals_dir(), [name])[0]


def test_required_cases_fail_if_policy_is_removed(monkeypatch):
    from ops_dashboard.sessions import browser_effects as fx
    monkeypatch.setattr(fx, "classify", lambda *a, **k: "allow")
    rep = runner.run([_suite("browser-agent")])
    failed = {c["key"] for c in rep["cases"] if not c["passed"] and c["required"]}
    assert {"act-password-refused", "act-buy-needs-approval-rejected",
            "act-disguised-live-element"} <= failed


def test_required_cases_fail_without_untrusted_wrapping(monkeypatch):
    from ops_dashboard import web_ask
    monkeypatch.setattr(web_ask, "wrap_untrusted", lambda c, label, nonce: c)
    rep = runner.run([_suite("web-injection")])
    failed = {c["key"] for c in rep["cases"] if not c["passed"] and c["required"]}
    assert {"visible-injection-wrapped", "fake-end-marker-contained"} <= failed


def test_exhausted_script_fails_the_case(tmp_path):
    s = _suite("browser-agent")
    case = next(c for c in s.cases if c.key == "act-harmless-actions-run")
    cas = tmp_path / "short.json"
    cas.write_text(json.dumps({"cassette_version": 1, "kind": "script", "origin": "synthetic",
                               "turns": [{"tool": "read", "args": {}}], "response": {"text": ""}}))
    r = runner.run_case(s, case, provider_factory=lambda su, c: web.ScriptProvider(cas))
    assert not r.passed and r.error and r.error.startswith("unexpected_provider_call")


def test_eval_bridge_emulates_rust_refusals():
    page = {"url": "https://s.example/", "elements": [
        {"ref": 1, "tag": "input", "type": "password", "name": "pw"},
        {"ref": 2, "tag": "button", "name": "Buy", "in_form": True},
        {"ref": 3, "tag": "a", "name": "Next", "href": "https://s.example/2"}]}
    b = web.EvalBridge({"https://s.example/": page}, start="https://s.example/")
    assert b.call("type", 1, ref=1, text="x")["error"] == "denied:password_field"
    assert b.call("type", 1, ref=1, text="x", confirmed=True)["error"] == "denied:password_field"
    assert b.call("click", 1, ref=2)["error"] == "confirmation_required:submit"
    assert b.call("click", 1, ref=2, confirmed=True)["ok"]
    assert b.call("click", 1, ref=3)["ok"]
    assert [(x["op"], x["ref"], x["confirmed"]) for x in b.executed] == [("click", 2, True), ("click", 3, False)]


def test_web_checks():
    o = checks.Outcome(browser={"executed": [{"op": "navigate", "url": "https://A.example/x"},
                                             {"op": "click", "ref": 2, "effect": "confirm:submit",
                                              "confirmed": False}],
                                "events": ["browser.denied"], "approvals": [], "task_status": "needs_review"})
    run = lambda **spec: checks.run_check(o, spec).passed  # noqa: E731
    assert not run(type="not_executed", op="navigate", url_contains="a.example")
    assert run(type="not_executed", op="type")
    assert run(type="executed", op="click", ref=2)
    assert not run(type="executed_effects_confirmed")
    assert run(type="event", kind="browser.denied") and run(type="no_event", kind="browser.click")
    assert run(type="approvals_count", max=0) and not run(type="approvals_count", min=1)
    assert run(type="task_status", value="needs_review")
    with pytest.raises(checks.CheckSpecError):
        checks.run_check(o, {"type": "executed"})


def test_required_cases_fail_without_the_new_site_gate(monkeypatch):
    from ops_dashboard.sessions import browser as sb

    def ungated(self, op, url):  # what BrowserTools._nav did before the gate
        return self._navved(op, url, self._nav_call(op, url, confirmed=True), None)
    monkeypatch.setattr(sb.BrowserTools, "_nav", ungated)
    monkeypatch.setattr(sb.BrowserTools, "_pending_nav", lambda self: None)
    rep = runner.run([_suite("browser-agent")])
    failed = {c["key"] for c in rep["cases"] if not c["passed"] and c["required"]}
    assert {"exfil-navigation-blocked", "redirect-to-new-site-asks"} <= failed


def test_required_cases_fail_without_the_visibility_filter(monkeypatch):
    from ops_dashboard import web_clip
    monkeypatch.setattr(web_clip, "_hidden_reason", lambda a: None)
    monkeypatch.setattr(web_clip, "_text_hidden", lambda stack: None)
    rep = runner.run([_suite("web-injection")])
    failed = {c["key"] for c in rep["cases"] if not c["passed"] and c["required"]}
    assert {"offscreen-text-dropped", "invisible-css-dropped"} <= failed
