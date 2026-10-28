"""Live evals with BYOK (learning plan L2, design §3.2).

Every test uses a FAKE provider injected through ``provider_builder`` (or a
monkeypatched ``live.build_provider``) and a fake secret store: no real key is
read and no real provider is called. Covers: opt-in only, refusal in CI,
key_ref mandatory and never from env vars, mandatory EUR budget and call
limit, price required, cut at 100 % of the budget, report comparable with the
offline baseline, local results (migration 25), redacted recordings outside
the repo, the optional judge, and no network.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from ops_dashboard import brain, db, secrets_store
from ops_dashboard.evals import __main__ as cli
from ops_dashboard.evals import live, netguard, record, runner

MODEL = "fake-model"
Q = "¿Cuánto cuesta el plan Pro?"
KEY_REF = "byok.anthropic.personal"


def _fake_key() -> str:
    return runner.fake_secret("anthropic_key")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    before = db.DB_PATH
    for name in record.CI_MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(live.OPT_IN_ENV, raising=False)
    monkeypatch.setenv("OPS_DISABLE_KEYCHAIN", "1")
    monkeypatch.setenv("OPS_SECRETS_DIR", str(tmp_path / "secrets"))
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BRAIN_PROVIDER", "BRAIN_MODEL"):
        monkeypatch.delenv(v, raising=False)
    db.DB_PATH = tmp_path / "user.db"
    db.init_db()
    yield tmp_path
    db.DB_PATH = before


def _price(provider: str = "anthropic", model: str = MODEL, inp: float = 300.0, out: float = 1500.0) -> None:
    from ops_dashboard.sessions import SessionStore
    SessionStore().set_model_price(provider, model, in_cents_per_mtok=inp, out_cents_per_mtok=out)


def _suite(tmp_path: Path, n: int = 3, target: str = "brain.ask", assertions: list[str] | None = None) -> Path:
    root = tmp_path / "evals"
    vault = root / "fixtures" / "vault"
    vault.mkdir(parents=True)
    (vault / "pricing.md").write_text("# Precios\n\nEl plan Pro cuesta 29 € al mes.\n", encoding="utf-8")
    sdir = root / "mini"
    (sdir / "cassettes").mkdir(parents=True)
    cases = []
    for i in range(n):
        cases.append({"id": i, "prompt": f"{Q} ({i})", "expected_output": "29 €", "files": [],
                      "assertions": assertions or [],
                      "conductloop": {"key": f"c{i}", "required": i == 0,
                                      "checks": [{"type": "must_contain", "text": "29 €"}, {"type": "no_secret"}]}})
    doc = {"skill_name": "mini", "conductloop": {"suite": "mini", "target": target,
                                                 "vault": "../fixtures/vault", "embedder": "stub"},
           "evals": cases}
    (sdir / "evals.json").write_text(json.dumps(doc), encoding="utf-8")
    (root / "baseline.json").write_text(json.dumps({
        "version": 1, "overall": {"passed": n, "total": n, "score": 100.0},
        "suites": {"mini": {"passed": n, "total": n, "score": 100.0}},
        "cases": {f"mini/c{i}": True for i in range(n)}}), encoding="utf-8")
    return root


class FakeLLM:
    """Stands in for a real provider. Never touches the network."""

    name = "anthropic"

    def __init__(self, answer: str = "El plan Pro cuesta 29 € al mes [1].", usage: dict | None = None,
                 fail: Exception | None = None, judge_json: str | None = None):
        self.model = MODEL
        self.api_key = _fake_key()
        self.answer, self.fail, self.judge_json = answer, fail, judge_json
        self.usage = {"input_tokens": 1000, "output_tokens": 100} if usage is None else usage
        self.last_usage: dict | None = None
        self.seen: list[dict] = []

    def ask(self, system, messages, **kw):
        self.seen.append({"system": system, "messages": messages, "kw": kw})
        if self.fail:
            raise self.fail
        self.last_usage = dict(self.usage) if self.usage else None
        if system == live.JUDGE_SYSTEM:
            return self.judge_json or '{"results": []}'
        return self.answer


def _cfg(**kw) -> live.LiveConfig:
    base = dict(provider="anthropic", model=MODEL, key_ref=KEY_REF, budget_eur=0.5, max_calls=20)
    base.update(kw)
    return live.validate_config(**base)


def _runs() -> list[dict]:
    with sqlite3.connect(db.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM eval_runs ORDER BY id")]


def _results() -> list[dict]:
    with sqlite3.connect(db.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM eval_results ORDER BY id")]


def _live_argv(root: Path, out: Path, *extra: str) -> list[str]:
    return ["run", "--live", "--evals-dir", str(root), "--provider", "anthropic", "--model", MODEL,
            "--key-ref", KEY_REF, "--budget-eur", "0.5", "--max-calls", "20", "--out-dir", str(out), *extra]


# ---------- never in CI, opt-in only ----------

@pytest.mark.parametrize("marker", ["CI", "GITHUB_ACTIONS", "BUILDKITE", "CONDUCTLOOP_NO_LIVE_EVALS"])
def test_live_refuses_in_ci_even_with_flag(env, monkeypatch, capsys, marker):
    root = _suite(env)
    _price()
    built = []
    real_build = live.build_provider
    monkeypatch.setattr(live, "build_provider", lambda cfg: built.append(cfg) or FakeLLM())
    monkeypatch.setenv(marker, "true")
    rc = cli.main(_live_argv(root, env / "out"))
    assert rc == 3 and "ci_detected" in capsys.readouterr().err
    assert built == [] and _runs() == [] and not (env / "out").exists()
    with pytest.raises(live.LiveRefused) as ei:
        live.run_live(runner.discover(root), _cfg(), provider_builder=lambda c: FakeLLM())
    assert ei.value.code == "ci_detected"
    with pytest.raises(live.LiveRefused) as ei:
        real_build(_cfg())
    assert ei.value.code == "ci_detected"


def test_ci_marker_set_to_false_is_not_ci(monkeypatch):
    monkeypatch.setenv("CI", "false")
    live.refuse_in_ci()


def test_plain_run_stays_offline(env, monkeypatch, capsys):
    built = []
    monkeypatch.setattr(live, "build_provider", lambda cfg: built.append(cfg) or FakeLLM())
    root = _suite(env, n=1)
    rc = cli.main(["run", "--evals-dir", str(root)])
    out = capsys.readouterr().out
    assert "Evals (offline)" in out and built == [] and _runs() == []
    assert rc == 1  # the mini suite has no cassette: offline fails, never goes live


def test_live_flags_without_live_are_rejected(env, capsys):
    root = _suite(env, n=1)
    rc = cli.main(["run", "--evals-dir", str(root), "--budget-eur", "1"])
    assert rc == 2 and "add --live" in capsys.readouterr().err


def test_env_opt_in_turns_live_on_and_still_needs_budget(env, monkeypatch, capsys):
    root = _suite(env, n=1)
    monkeypatch.setenv(live.OPT_IN_ENV, "1")
    rc = cli.main(["run", "--evals-dir", str(root), "--provider", "anthropic", "--model", MODEL,
                   "--key-ref", KEY_REF, "--max-calls", "3"])
    assert rc == 3 and "budget_required" in capsys.readouterr().err


def test_ci_workflows_never_run_live_evals():
    wf = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for f in wf.glob("*.y*ml"):
        text = f.read_text(encoding="utf-8")
        assert "--live" not in text and live.OPT_IN_ENV not in text, f.name


# ---------- configuration ----------

@pytest.mark.parametrize("kw,code", [
    ({"budget_eur": None}, "budget_required"),
    ({"budget_eur": 0}, "bad_budget"),
    ({"budget_eur": -1.0}, "bad_budget"),
    ({"budget_eur": math.nan}, "bad_budget"),
    ({"budget_eur": math.inf}, "bad_budget"),
    ({"budget_eur": 50.0}, "bad_budget"),
    ({"max_calls": None}, "max_calls_required"),
    ({"max_calls": 0}, "bad_max_calls"),
    ({"max_calls": 10_000}, "bad_max_calls"),
    ({"key_ref": None}, "key_ref_required"),
    ({"model": ""}, "model_required"),
    ({"provider": "mock"}, "bad_provider"),
    ({"provider": "auto"}, "bad_provider"),
    ({"provider": "ollama"}, "key_ref_unsupported"),
    ({"max_output_tokens": 0}, "bad_max_output_tokens"),
])
def test_config_validation(kw, code):
    with pytest.raises(live.LiveRefused) as ei:
        _cfg(**kw)
    assert ei.value.code == code


def test_ollama_needs_no_key_ref():
    cfg = _cfg(provider="ollama", key_ref=None)
    assert cfg.key_ref is None and cfg.budget_cents == 50.0


def test_key_value_passed_as_key_ref_is_refused_without_echo():
    k = _fake_key()
    with pytest.raises(live.LiveRefused) as ei:
        _cfg(key_ref=k)
    assert ei.value.code == "bad_key_ref" and k not in str(ei.value)


def test_record_dir_inside_repo_sources_is_refused(env):
    with pytest.raises(live.LiveRefused) as ei:
        _cfg(record_dir=live.repo_root() / "evals" / "mini" / "cassettes")
    assert ei.value.code == "record_dir_in_repo"
    _cfg(record_dir=env / "rec")  # outside the repo: fine


# ---------- BYOK: key_ref only, never env ----------

def test_build_provider_uses_key_ref_never_env(monkeypatch):
    env_key = "sk-ant-" + "envkeyshouldneverbeused0000"
    monkeypatch.setenv("ANTHROPIC_API_KEY", env_key)
    asked = []
    monkeypatch.setattr(secrets_store, "get_secret", lambda name: asked.append(name) or None)
    with pytest.raises(live.LiveRefused) as ei:
        live.build_provider(_cfg())
    assert ei.value.code == "provider_unavailable:key_missing" and asked == [KEY_REF]
    assert env_key not in str(ei.value)
    monkeypatch.setattr(secrets_store, "get_secret", lambda name: _fake_key() if name == KEY_REF else None)
    p = live.build_provider(_cfg())
    assert isinstance(p, brain.AnthropicProvider) and p.api_key == _fake_key() and p.model == MODEL


def test_build_provider_openai_uses_key_ref(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "envkeyshouldneverbeused000000000000")
    monkeypatch.setattr(secrets_store, "get_secret", lambda name: "store-key" if name == "byok.openai.me" else None)
    p = live.build_provider(_cfg(provider="openai", key_ref="byok.openai.me"))
    assert isinstance(p, brain.OpenAIProvider) and p.api_key == "store-key"


def test_build_provider_no_fallback_when_in_cooldown(monkeypatch):
    monkeypatch.setattr(secrets_store, "get_secret", lambda name: "store-key")
    monkeypatch.setattr(brain, "_provider_is_in_cooldown", lambda name: True)
    with pytest.raises(live.LiveRefused) as ei:
        live.build_provider(_cfg())
    assert ei.value.code.startswith("provider_unavailable:")


# ---------- price + budget + call limit ----------

def test_price_missing_refuses_before_any_call(env):
    root = _suite(env, n=1)
    built = []
    with pytest.raises(live.LiveRefused) as ei:
        live.run_live(runner.discover(root), _cfg(), provider_builder=lambda c: built.append(c) or FakeLLM())
    assert ei.value.code == "price_missing" and built == [] and _runs() == []


def test_budget_cuts_before_crossing_100_percent(env):
    root = _suite(env, n=6)
    _price()
    fake = FakeLLM()
    rep = live.run_live(runner.discover(root), _cfg(budget_eur=0.03), provider_builder=lambda c: fake,
                        report_dir=env / "out")
    assert rep["complete"] is False and rep["stop_reason"] == "budget_exhausted"
    assert rep["cost"]["spent_cents"] <= 3.0 and 0 < len(fake.seen) < 6
    assert rep["not_run"] == 6 - len(fake.seen)
    assert [c["status"] for c in rep["cases"]].count("not_run") == rep["not_run"]
    # every call carried the output cap the projection assumed
    assert all(s["kw"]["max_tokens"] == live.DEFAULT_MAX_OUTPUT_TOKENS for s in fake.seen)


def test_reaching_100_percent_stops_the_run(env):
    root = _suite(env, n=4)
    _price()
    fake = FakeLLM(usage={"input_tokens": 1000, "output_tokens": 50_000})  # provider ignored max_tokens
    rep = live.run_live(runner.discover(root), _cfg(budget_eur=0.05), provider_builder=lambda c: fake)
    assert len(fake.seen) == 1 and rep["stop_reason"] == "budget_exhausted" and rep["not_run"] == 3
    assert rep["cost"]["spent_cents"] >= 5.0  # overshoot is at most the one call already in flight


def test_first_call_over_budget_makes_no_call(env):
    root = _suite(env, n=2)
    _price(inp=10_000_000, out=10_000_000)
    fake = FakeLLM()
    rep = live.run_live(runner.discover(root), _cfg(budget_eur=0.01), provider_builder=lambda c: fake)
    assert fake.seen == [] and rep["not_run"] == 2 and rep["cost"]["spent_cents"] == 0


def test_call_limit_stops_the_run(env):
    root = _suite(env, n=5)
    _price()
    fake = FakeLLM()
    rep = live.run_live(runner.discover(root), _cfg(max_calls=2), provider_builder=lambda c: fake)
    assert len(fake.seen) == 2 and rep["cost"]["calls"] == 2
    assert rep["stop_reason"] == "call_limit" and rep["not_run"] == 3


def test_missing_usage_is_estimated_and_flagged(env):
    root = _suite(env, n=1)
    _price()
    rep = live.run_live(runner.discover(root), _cfg(), provider_builder=lambda c: FakeLLM(usage={}))
    c = rep["cases"][0]
    assert c["usage"]["estimated"] and c["usage"]["input_tokens"] > 0 and rep["cost"]["usage_estimated"]
    assert rep["cost"]["spent_cents"] > 0


def test_ollama_is_free_but_still_call_limited(env):
    root = _suite(env, n=3)
    fake = FakeLLM()
    fake.name = "ollama"
    rep = live.run_live(runner.discover(root), _cfg(provider="ollama", key_ref=None, max_calls=2),
                        provider_builder=lambda c: fake)
    assert rep["cost"]["spent_cents"] == 0 and len(fake.seen) == 2 and rep["stop_reason"] == "call_limit"


def test_invalid_key_stops_the_run(env):
    root = _suite(env, n=3)
    _price()
    fake = FakeLLM(fail=RuntimeError("Error code: 401 - invalid_api_key"))
    rep = live.run_live(runner.discover(root), _cfg(), provider_builder=lambda c: fake)
    assert len(fake.seen) == 1 and rep["stop_reason"] == "provider_error:invalid_key"
    assert rep["cases"][0]["status"] == "run" and rep["cases"][0]["error"] == "provider_error: invalid_key"
    assert rep["not_run"] == 2


# ---------- report: comparable with the offline baseline, stored locally ----------

def test_report_is_comparable_with_offline_baseline(env):
    root = _suite(env, n=3)
    _price()
    fake = FakeLLM()
    calls = iter(["El plan Pro cuesta 29 € al mes [1].", "No lo sé.", "Son 29 € al mes."])
    fake.ask = lambda system, messages, **kw: (fake.seen.append({"kw": kw}), setattr(  # type: ignore[method-assign]
        fake, "last_usage", {"input_tokens": 10, "output_tokens": 5}), next(calls))[2]
    baseline = json.loads((root / "baseline.json").read_text(encoding="utf-8"))
    rep = live.run_live(runner.discover(root), _cfg(), baseline=baseline, provider_builder=lambda c: fake,
                        report_dir=env / "out")
    offline = runner.run(runner.discover(root))
    assert {"version", "overall", "suites", "cases"} <= set(rep) and set(offline["overall"]) == set(rep["overall"])
    assert rep["mode"] == "live" and rep["overall"] == {"passed": 2, "total": 3, "score": 66.67}
    assert rep["vs_offline"] == {"same": 2, "regressed": 1, "improved": 0, "new": 0, "not_run": 0}
    assert rep["cases"][1]["vs_offline"] == "regressed" and rep["cases"][1]["offline_baseline"] is True
    assert rep["complete"] and rep["ok"] is False  # 33-point drop vs the offline baseline (D10)
    assert any("score drop" in p for p in rep["problems"])
    md = Path(rep["report_path"]).read_text(encoding="utf-8")
    assert "Evals (live): FAIL" in md and "| mini | 66.67 | 2/3 | 100.00 | -33.33 |" in md
    js = json.loads(Path(rep["report_path"]).with_suffix(".json").read_text(encoding="utf-8"))
    assert js["overall"] == rep["overall"]
    [run] = _runs()
    assert run["mode"] == "live" and run["status"] == "completed" and run["key_ref"] == KEY_REF
    assert (run["passed"], run["failed"], run["calls"]) == (2, 1, 3) and run["report_path"] == rep["report_path"]
    assert run["budget_cost_cents"] == 50.0 and run["cost_cents_est"] > 0
    res = _results()
    assert [r["case_id"] for r in res] == ["mini/c0", "mini/c1", "mini/c2"] and res[1]["passed"] == 0


def test_cli_live_end_to_end_with_fake_provider(env, monkeypatch, capsys):
    root = _suite(env, n=2)
    _price()
    monkeypatch.setattr(live, "build_provider", lambda cfg: FakeLLM())
    rc = cli.main(_live_argv(root, env / "out", "--report", str(env / "r.json")))
    out = capsys.readouterr().out
    assert rc == 0 and "Evals (live): PASS" in out and "report:" in out
    assert json.loads((env / "r.json").read_text(encoding="utf-8"))["overall"]["score"] == 100.0
    [run] = _runs()
    assert run["status"] == "completed" and run["passed"] == 2


def test_cli_incomplete_run_exits_4(env, monkeypatch, capsys):
    root = _suite(env, n=3)
    _price()
    monkeypatch.setattr(live, "build_provider", lambda cfg: FakeLLM())
    argv = _live_argv(root, env / "out")
    argv[argv.index("--max-calls") + 1] = "1"
    assert cli.main(argv) == 4
    assert _runs()[0]["status"] == "stopped" and _runs()[0]["stop_reason"] == "call_limit"


def test_skipped_suites_and_no_live_suite(env):
    root = _suite(env, n=1, target="db.search")
    _price()
    with pytest.raises(live.LiveRefused) as ei:
        live.run_live(runner.discover(root), _cfg(), provider_builder=lambda c: FakeLLM())
    assert ei.value.code == "no_live_suites"
    keep, skipped = live.live_suites(runner.discover(runner.default_evals_dir()))
    assert {s.name for s in keep} == {"brain-rag", "web-injection"}
    assert {s["suite"] for s in skipped} == {"search", "browser-agent"}


def test_repo_web_injection_case_runs_live_with_fake(env):
    suite = runner.discover(runner.default_evals_dir(), ["web-injection"])[0]
    suite.cases = suite.cases[:1]
    _price()
    fake = FakeLLM(answer="Resumen de la página.")
    rep = live.run_live([suite], _cfg(), provider_builder=lambda c: fake)
    assert len(fake.seen) == 1 and rep["cases"][0]["status"] == "run"
    assert "UNTRUSTED" in fake.seen[0]["system"] + json.dumps(fake.seen[0]["messages"])


# ---------- secrets, recording, judge, network ----------

def test_key_value_never_lands_in_reports_db_cassettes_or_stdout(env, monkeypatch, capsys):
    root = _suite(env, n=2)
    _price()
    k = _fake_key()
    monkeypatch.setattr(secrets_store, "get_secret", lambda name: k if name == KEY_REF else None)
    monkeypatch.setattr(live, "build_provider", lambda cfg: FakeLLM(answer=f"29 € al mes. La clave es {k}"))
    rc = cli.main(_live_argv(root, env / "out", "--record", "--record-dir", str(env / "rec"),
                             "--report", str(env / "r.json")))
    out = capsys.readouterr()
    assert rc == 1  # no_secret check fails: the answer leaked a key-shaped string
    blobs = [out.out, out.err] + [p.read_text(encoding="utf-8") for p in env.rglob("*")
                                  if p.is_file() and p.suffix in (".md", ".json")]
    blobs.append(db.DB_PATH.read_bytes().decode("utf-8", "replace"))
    assert len(blobs) > 5 and all(k not in b for b in blobs)
    cas = list((env / "rec").rglob("*.json"))
    assert len(cas) == 2 and all("redacted" in c.read_text(encoding="utf-8").lower() for c in cas)


def test_recorded_cassette_replays_offline(env):
    root = _suite(env, n=1)
    _price()
    rep = live.run_live(runner.discover(root), _cfg(record_dir=env / "rec"), provider_builder=lambda c: FakeLLM())
    out = Path(rep["cases"][0]["recorded"])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["origin"] == "recorded" and data["model"] == MODEL and data["response"]["usage"]["output_tokens"] == 100
    assert not live._in_repo_sources(out)
    (root / "mini" / "cassettes" / "c0.json").write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    assert runner.run(runner.discover(root))["cases"][0]["passed"]


def test_record_skips_request_with_secret(env):
    root = _suite(env, n=1)
    doc = json.loads((root / "mini" / "evals.json").read_text(encoding="utf-8"))
    doc["evals"][0]["prompt"] = "mi clave es " + runner.fake_secret("github_token")
    (root / "mini" / "evals.json").write_text(json.dumps(doc), encoding="utf-8")
    _price()
    rep = live.run_live(runner.discover(root), _cfg(record_dir=env / "rec"), provider_builder=lambda c: FakeLLM())
    assert "not recorded (secret_in_request)" in rep["cases"][0]["warnings"] and not (env / "rec").exists()


def test_judge_scores_assertions_within_the_same_budget(env):
    root = _suite(env, n=1, assertions=["Da el precio mensual", "Cita la nota"])
    _price()
    fake = FakeLLM(judge_json='{"results": [{"i": 0, "passed": true, "evidence": "29 €"},'
                              ' {"i": 1, "passed": false, "evidence": "sin cita"}]}')
    rep = live.run_live(runner.discover(root), _cfg(judge=True), provider_builder=lambda c: fake)
    j = rep["cases"][0]["judge"]
    assert (j["passed"], j["total"]) == (1, 2) and rep["cases"][0]["passed"] is True  # judge never flips pass/fail
    assert rep["cost"]["calls"] == 2 and len(fake.seen) == 2 and j["cost_cents_est"] > 0
    assert "<answer>" in fake.seen[1]["messages"][0]["content"]


def test_judge_bad_json_is_reported_not_fatal(env):
    root = _suite(env, n=1, assertions=["x"])
    _price()
    rep = live.run_live(runner.discover(root), _cfg(judge=True),
                        provider_builder=lambda c: FakeLLM(judge_json="no json"))
    assert "error" in rep["cases"][0]["judge"] and rep["complete"]


def test_live_run_with_fake_provider_opens_no_socket(env):
    root = _suite(env, n=2)
    _price()
    with netguard.blocked() as guard:
        rep = live.run_live(runner.discover(root), _cfg(), provider_builder=lambda c: FakeLLM(),
                            report_dir=env / "out")
    assert guard.attempts == [] and rep["overall"]["passed"] == 2


# ---------- migration 25 + Aprendizaje view ----------

def test_migration_25_is_idempotent_and_view_hides_key_ref(env):
    from ops_dashboard import migrations
    from ops_dashboard.learning import evals_view
    with sqlite3.connect(db.DB_PATH) as c:
        migrations._mig_25_eval_runs(c)
        assert migrations.current_version(c) >= 25
    root = _suite(env, n=1)
    _price()
    live.run_live(runner.discover(root), _cfg(), provider_builder=lambda c: FakeLLM())
    [r] = evals_view.local_runs()
    assert r["mode"] == "live" and r["passed"] == 1 and "key_ref" not in r
