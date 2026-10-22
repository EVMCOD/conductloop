"""Tests for the offline eval runner (learning plan L1).

Covers: the repo suites match the committed baseline; missing / mismatched
cassettes fail; a failing required case fails the run; a >5-point score drop
fails (D10); secrets are detected without leaking their value; the network is
blocked; recording refuses in CI and never reads keys from env vars (D9).
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from ops_dashboard import brain, db
from ops_dashboard.evals import __main__ as cli
from ops_dashboard.evals import checks, netguard, record, runner

REPO_EVALS = runner.default_evals_dir()


@pytest.fixture(autouse=True)
def _restore_db_path():
    before = db.DB_PATH
    yield
    db.DB_PATH = before


def _no_ci(monkeypatch):
    for name in record.CI_MARKERS:
        monkeypatch.delenv(name, raising=False)


def _suite(tmp_path: Path, cases: list[dict], cassettes: dict[str, dict | None],
           target: str = "brain.ask") -> Path:
    """Write a one-suite evals dir with a two-note vault."""
    root = tmp_path / "evals"
    vault = root / "fixtures" / "vault"
    vault.mkdir(parents=True)
    (vault / "pricing.md").write_text("# Precios\n\nEl plan Pro cuesta 29 € al mes.\n", encoding="utf-8")
    (vault / "other.md").write_text("# Otra\n\nNada que ver con nada.\n", encoding="utf-8")
    sdir = root / "mini"
    (sdir / "cassettes").mkdir(parents=True)
    doc = {"skill_name": "mini", "conductloop": {"suite": "mini", "target": target,
                                                 "vault": "../fixtures/vault", "embedder": "stub"},
           "evals": cases}
    (sdir / "evals.json").write_text(json.dumps(doc), encoding="utf-8")
    for key, cas in cassettes.items():
        if cas is not None:
            (sdir / "cassettes" / f"{key}.json").write_text(json.dumps(cas), encoding="utf-8")
    return root


def _case(key: str, prompt: str, chks: list[dict], required: bool = True) -> dict:
    return {"id": key, "prompt": prompt, "expected_output": "x", "files": [], "assertions": [],
            "conductloop": {"key": key, "required": required, "checks": chks}}


def _cassette(key: str, prompt: str, text: str) -> dict:
    return {"cassette_version": 1, "case_id": key, "origin": "synthetic", "provider": "synthetic",
            "model": "synthetic", "request": {"messages": [{"role": "user", "content": prompt}]},
            "response": {"text": text, "usage": {"output_tokens": 10}}}


Q = "¿Cuánto cuesta el plan Pro?"


# ---------- repo suites ----------

def test_repo_suites_match_committed_baseline():
    suites = runner.discover(REPO_EVALS)
    report = runner.run(suites)
    baseline = json.loads((REPO_EVALS / "baseline.json").read_text(encoding="utf-8"))
    ok, problems, _ = runner.compare(report, baseline)
    assert ok, problems
    # Deterministic: the same cases pass as when the baseline was written.
    got = {f"{c['suite']}/{c['key']}": c["passed"] for c in report["cases"]}
    assert got == baseline["cases"]
    assert report["overall"]["score"] == baseline["overall"]["score"]
    # every non-passing repo case is a documented known gap, never a required case
    for c in report["cases"]:
        if not c["passed"]:
            assert c["known_gap"] and not c["required"], c


def test_repo_suites_follow_agentskills_evals_shape():
    for path in sorted(REPO_EVALS.glob("*/evals.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data["skill_name"], str) and data["evals"]
        for e in data["evals"]:
            assert {"id", "prompt", "expected_output"} <= set(e)
            assert isinstance(e.get("files", []), list) and isinstance(e.get("assertions", []), list)


def test_repo_cassettes_are_synthetic_and_clean():
    for path in sorted(REPO_EVALS.glob("*/cassettes/*.json")):
        raw = path.read_text(encoding="utf-8")
        assert not checks.find_secrets(raw), path
        assert json.loads(raw)["origin"] in ("synthetic", "recorded")


# ---------- runner failure modes ----------

def test_missing_cassette_fails_the_case_and_the_run(tmp_path):
    root = _suite(tmp_path, [_case("c1", Q, [{"type": "must_contain", "text": "29"}])], {"c1": None})
    report = runner.run(runner.discover(root))
    case = report["cases"][0]
    assert not case["passed"] and case["error"].startswith("missing_cassette")
    ok, problems, _ = runner.compare(report, None)
    assert not ok and "required case failed: mini/c1" in problems[0]


def test_mismatched_cassette_fails(tmp_path):
    root = _suite(tmp_path, [_case("c1", Q, [{"type": "must_contain", "text": "29"}])],
                  {"c1": _cassette("c1", "otra pregunta", "29 €")})
    case = runner.run(runner.discover(root))["cases"][0]
    assert case["error"].startswith("cassette_mismatch")


def test_failing_required_case_fails_run_and_cli_exit_code(tmp_path, capsys):
    root = _suite(tmp_path, [
        _case("good", Q, [{"type": "must_contain", "text": "29 €"}, {"type": "citation_path", "path": "pricing.md",
                                                                      "within_top": 1}]),
        _case("bad", Q, [{"type": "must_contain", "text": "49 €"}]),
    ], {"good": _cassette("good", Q, "29 € al mes [1]"), "bad": _cassette("bad", Q, "29 € al mes [1]")})
    report = runner.run(runner.discover(root))
    assert [c["passed"] for c in report["cases"]] == [True, False]
    ok, problems, _ = runner.compare(report, None)
    assert not ok and any("mini/bad" in p for p in problems)
    rep = tmp_path / "report.json"
    summ = tmp_path / "summary.md"
    rc = cli.main(["run", "--evals-dir", str(root), "--report", str(rep), "--summary", str(summ)])
    assert rc == 1
    assert json.loads(rep.read_text())["ok"] is False
    assert "FAIL" in summ.read_text() and "mini/bad" in summ.read_text()


def test_non_required_failure_passes_without_baseline(tmp_path):
    root = _suite(tmp_path, [_case("gap", Q, [{"type": "must_contain", "text": "49 €"}], required=False)],
                  {"gap": _cassette("gap", Q, "29 €")})
    ok, problems, _ = runner.compare(runner.run(runner.discover(root)), None)
    assert ok and not problems


def _report(scores: dict[str, float], overall: float) -> dict:
    return {"overall": {"score": overall}, "suites": {k: {"score": v} for k, v in scores.items()}, "cases": []}


def test_score_drop_over_five_points_fails():
    base = _report({"a": 80.0, "b": 90.0}, 85.0)
    ok, problems, _ = runner.compare(_report({"a": 74.9, "b": 90.0}, 82.4), base)
    assert not ok and any("suite a" in p for p in problems)
    ok, problems, _ = runner.compare(_report({"a": 80.0, "b": 90.0}, 79.9), base)
    assert not ok and any("overall" in p for p in problems)


def test_score_drop_of_exactly_five_points_passes():
    base = _report({"a": 80.0}, 80.0)
    ok, problems, _ = runner.compare(_report({"a": 75.0}, 75.0), base)
    assert ok, problems


def test_suite_missing_from_run_fails_and_new_suite_is_a_note():
    ok, problems, _ = runner.compare(_report({"b": 90.0}, 90.0), _report({"a": 90.0, "b": 90.0}, 90.0))
    assert not ok and "suite in baseline but not run: a" in problems
    ok, _, notes = runner.compare(_report({"a": 90.0, "c": 50.0}, 90.0), _report({"a": 90.0}, 90.0))
    assert ok and any("new suite" in n for n in notes)


def test_update_baseline_refused_in_ci(tmp_path, monkeypatch):
    root = _suite(tmp_path, [_case("c1", Q, [{"type": "must_contain", "text": "29"}])],
                  {"c1": _cassette("c1", Q, "29 €")})
    monkeypatch.setenv("CI", "true")
    assert cli.main(["run", "--evals-dir", str(root), "--update-baseline"]) == 2
    assert not (root / "baseline.json").exists()


def test_unknown_check_type_is_a_suite_error(tmp_path):
    root = _suite(tmp_path, [_case("c1", Q, [{"type": "vibes"}])], {"c1": None})
    with pytest.raises(runner.SuiteError):
        runner.discover(root)
    assert cli.main(["run", "--evals-dir", str(root)]) == 2


def test_case_env_is_restored(tmp_path):
    before_db, before_resolve = db.DB_PATH, brain.resolve_provider
    root = _suite(tmp_path, [_case("c1", Q, [{"type": "must_contain", "text": "29"}])],
                  {"c1": _cassette("c1", Q, "29 €")})
    assert runner.run(runner.discover(root))["cases"][0]["passed"]
    assert db.DB_PATH == before_db and brain.resolve_provider is before_resolve


# ---------- secrets ----------

@pytest.mark.parametrize("kind", ["anthropic_key", "openai_key", "stripe_secret", "github_token"])
def test_secret_detected_without_leaking_value(kind):
    secret = runner.fake_secret(kind)
    o = checks.Outcome(answer=f"tu clave es {secret}", prompt=f"ctx {secret}")
    for t in ("no_secret", "prompt_no_secret"):
        r = checks.run_check(o, {"type": t})
        assert not r.passed and kind in r.evidence and secret not in r.evidence
    assert checks.run_check(checks.Outcome(answer="sin claves"), {"type": "no_secret"}).passed


def test_secret_in_answer_fails_a_case(tmp_path):
    root = _suite(tmp_path, [_case("leak", Q, [{"type": "no_secret"}])],
                  {"leak": _cassette("leak", Q, "clave: " + runner.fake_secret("anthropic_key"))})
    case = runner.run(runner.discover(root))["cases"][0]
    assert not case["passed"]
    assert runner.fake_secret("anthropic_key") not in json.dumps(case)


# ---------- network ----------

class _NetProvider:
    name = "net"
    model = "net"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    def supports_tools(self) -> bool:
        return False

    def ask(self, system, messages, **kw):
        self.calls.append({"system": system, "messages": messages})
        socket.create_connection(("api.anthropic.com", 443), timeout=1)
        return "unreachable"


def test_network_is_blocked_and_fails_the_case(tmp_path):
    root = _suite(tmp_path, [_case("c1", Q, [{"type": "must_contain", "text": "x"}])], {"c1": None})
    report = runner.run(runner.discover(root), provider_factory=lambda s, c: _NetProvider())
    assert report["cases"][0]["error"].startswith("network_blocked")


def test_network_blocked_even_if_exception_is_swallowed(tmp_path):
    class Swallow(_NetProvider):
        def ask(self, system, messages, **kw):
            self.calls.append({"system": system, "messages": messages})
            try:
                socket.getaddrinfo("example.com", 443)
            except Exception:
                pass
            return "x"
    root = _suite(tmp_path, [_case("c1", Q, [{"type": "must_contain", "text": "x"}])], {"c1": None})
    report = runner.run(runner.discover(root), provider_factory=lambda s, c: Swallow())
    assert report["cases"][0]["error"].startswith("network_blocked")


def test_netguard_blocks_httpx_and_restores():
    import httpx
    orig = socket.create_connection
    with netguard.blocked() as g, pytest.raises((netguard.NetworkBlocked, httpx.HTTPError)):
        httpx.get("http://example.invalid/", timeout=1)
    assert g.attempts
    assert socket.create_connection is orig


# ---------- recording (D9) ----------

def test_record_refuses_in_ci(tmp_path, monkeypatch):
    _no_ci(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(record.RecordRefused) as ei:
        record.refuse_in_ci()
    assert ei.value.code == "ci_detected"
    rc = cli.main(["record", "--evals-dir", str(REPO_EVALS), "--suite", "brain-rag",
                   "--case", "rag-pricing-cite", "--provider", "anthropic", "--key-name", "x",
                   "--out-dir", str(tmp_path)])
    assert rc == 3 and not any(tmp_path.iterdir())


def test_record_never_reads_env_keys(tmp_path, monkeypatch):
    _no_ci(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "envkeyshouldneverbeused0000")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "envkeyshouldneverbeused000000000000")
    from ops_dashboard import secrets_store
    monkeypatch.setattr(secrets_store, "get_secret", lambda name: None)
    for prov in ("anthropic", "openai"):
        with pytest.raises(record.RecordRefused) as ei:
            record.build_provider(prov, None, "eval_key")
        assert ei.value.code == "key_not_found"
    with pytest.raises(record.RecordRefused) as ei:
        record.build_provider("anthropic", None, None)
    assert ei.value.code == "key_required"


def test_record_uses_secret_store_key(monkeypatch):
    _no_ci(monkeypatch)
    from ops_dashboard import secrets_store
    monkeypatch.setattr(secrets_store, "get_secret", lambda name: "store-key" if name == "eval_key" else None)
    p = record.build_provider("anthropic", "m", "eval_key")
    assert isinstance(p, brain.AnthropicProvider) and p.api_key == "store-key"


class _FakeReal:
    name = "anthropic"
    model = "fake-model"
    last_usage = {"input_tokens": 100, "output_tokens": 9}

    def ask(self, system, messages, **kw):
        return "29 € al mes [1]. Clave vista: " + runner.fake_secret("anthropic_key")


def test_record_writes_redacted_cassette_that_replays(tmp_path, monkeypatch):
    _no_ci(monkeypatch)
    root = _suite(tmp_path, [_case("c1", Q, [{"type": "must_contain", "text": "29 €"}, {"type": "no_secret"}])],
                  {"c1": None})
    suite = runner.discover(root)[0]
    out = record.record_case(suite, suite.cases[0], provider="anthropic", model=None, key_name="k",
                             out_dir=tmp_path / "rec", provider_builder=lambda *a: _FakeReal())
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["origin"] == "recorded" and data["model"] == "fake-model"
    assert "[REDACTED:anthropic_key]" in data["response"]["text"]
    assert not checks.find_secrets(out.read_text(encoding="utf-8"))
    assert data["response"]["usage"]["output_tokens"] == 9 and data["system_sha256"]
    # a reviewed recording, copied into the suite, replays offline and passes
    (suite.dir / "cassettes" / "c1.json").write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    case = runner.run(runner.discover(root))["cases"][0]
    assert case["passed"], case


def test_record_refuses_request_with_secret(tmp_path, monkeypatch):
    _no_ci(monkeypatch)
    prompt = "mi clave es " + runner.fake_secret("github_token")
    root = _suite(tmp_path, [_case("c1", prompt, [{"type": "no_secret"}])], {"c1": None})
    suite = runner.discover(root)[0]
    with pytest.raises(record.RecordRefused) as ei:
        record.record_case(suite, suite.cases[0], provider="anthropic", model=None, key_name="k",
                           out_dir=tmp_path / "rec", provider_builder=lambda *a: _FakeReal())
    assert ei.value.code == "secret_in_request" and not (tmp_path / "rec").exists()
