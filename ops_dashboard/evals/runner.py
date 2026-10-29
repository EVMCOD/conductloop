"""Offline eval runner: load suites, build an isolated environment per case,
replay cassettes through the real code path, score, compare to the baseline.

Isolation per case: a fresh SQLite DB in a temp dir, the fixture vault copied
and indexed with the real Obsidian indexer, a deterministic embedder (or the
"embeddings unavailable" path of the lean build), optional brain_memory and
revenue fixtures. The provider is a ``CassetteProvider``; the network is
blocked for the whole run (``netguard``).

Baseline policy (decision D10): the run fails if any ``required`` case fails,
or if the score of any suite, or overall, drops more than ``MAX_DROP_POINTS``
below ``evals/baseline.json``.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cassette as _cassette
from . import checks as _checks
from . import netguard as _netguard

MAX_DROP_POINTS = 5.0
REPORT_VERSION = 1
TARGETS = ("brain.ask", "db.search", "web.ask_page", "browser.agent")  # web.*: browser PR 10
EMBEDDERS = ("stub", "unavailable")


def default_evals_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "evals"


class SuiteError(ValueError):
    """A suite file is malformed (bad JSON, unknown target/check, duplicate key)."""


# ---------- fake secrets for fixtures ----------

_FAKE_SECRET_RE = re.compile(r"\$\{FAKE_SECRET:([a-z_]+)\}")


def fake_secret(kind: str) -> str:
    """Deterministic, obviously-fake credential that matches ``checks.SECRET_PATTERNS``.
    Built at runtime so no credential-shaped string is committed to the repo."""
    tail = hashlib.sha256(f"conductloop-eval-{kind}".encode()).hexdigest()
    makers: dict[str, Callable[[], str]] = {
        "anthropic_key": lambda: "sk-ant-" + "evalfake" + tail[:32],
        "openai_key": lambda: "sk-" + "evalfake" + tail[:40],
        "stripe_secret": lambda: "sk_" + "test_" + "evalfake" + tail[:24],
        "github_token": lambda: "ghp_" + "evalfake" + tail[:32],
    }
    if kind not in makers:
        raise SuiteError(f"unknown FAKE_SECRET kind {kind!r}")
    return makers[kind]()


def expand_placeholders(text: str) -> str:
    return _FAKE_SECRET_RE.sub(lambda m: fake_secret(m.group(1)), text)


# ---------- deterministic embedder ----------

_STOP = frozenset(
    "about after also and are con como cual cuál del desde donde dónde este esta for from has have "
    "las los mis nos para pero por que qué sin sobre son the this una uno what with".split()
)
_DIM = 4096


def stub_embed(text: str) -> list[float]:
    """Hashed bag-of-words, L2-normalised. Deterministic stand-in for the
    sentence-transformers model so retrieval ranking is testable offline."""
    vec = [0.0] * _DIM
    for tok in re.findall(r"\w+", (text or "").lower()):
        if len(tok) < 3 or tok in _STOP:
            continue
        h = int(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).hexdigest(), 16)
        vec[h % _DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


# ---------- suites ----------

@dataclass
class Case:
    suite: str
    id: Any
    key: str
    prompt: str
    expected_output: str
    files: list[str]
    assertions: list[str]
    required: bool
    known_gap: str | None
    checks: list[dict]
    fixtures: dict
    params: dict

    @property
    def qualified(self) -> str:
        return f"{self.suite}/{self.key}"


@dataclass
class Suite:
    name: str
    dir: Path
    target: str
    vault: Path | None
    embedder: str
    cases: list[Case]


_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")


def load_suite(path: Path) -> Suite:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SuiteError(f"{path}: {e}") from e
    cl = data.get("conductloop") or {}
    name = str(cl.get("suite") or path.parent.name)
    target = str(cl.get("target") or "")
    if target not in TARGETS:
        raise SuiteError(f"{path}: unknown target {target!r} (expected one of {TARGETS})")
    embedder = str(cl.get("embedder") or "stub")
    if embedder not in EMBEDDERS:
        raise SuiteError(f"{path}: unknown embedder {embedder!r}")
    vault = (path.parent / cl["vault"]).resolve() if cl.get("vault") else None
    if vault is not None and not vault.is_dir():
        raise SuiteError(f"{path}: vault dir not found: {vault}")
    cases: list[Case] = []
    seen: set[str] = set()
    for raw in data.get("evals") or []:
        ccl = raw.get("conductloop") or {}
        key = str(ccl.get("key") or raw.get("id"))
        if not _KEY_RE.match(key) or key in seen:
            raise SuiteError(f"{path}: invalid or duplicate case key {key!r}")
        seen.add(key)
        if not isinstance(raw.get("prompt"), str) or not raw["prompt"].strip():
            raise SuiteError(f"{path}: case {key} has no prompt")
        checks = list(ccl.get("checks") or [])
        if not checks:
            raise SuiteError(f"{path}: case {key} has no checks")
        for c in checks:
            if c.get("type") not in _checks.CHECK_TYPES:
                raise SuiteError(f"{path}: case {key}: unknown check {c.get('type')!r}")
        for f in raw.get("files") or []:
            if not (path.parent / f).exists():
                raise SuiteError(f"{path}: case {key}: listed file missing: {f}")
        fixtures = dict(ccl.get("fixtures") or {})
        if fixtures.get("embedder", embedder) not in EMBEDDERS:
            raise SuiteError(f"{path}: case {key}: unknown embedder")
        cases.append(Case(
            suite=name, id=raw.get("id"), key=key, prompt=raw["prompt"],
            expected_output=str(raw.get("expected_output") or ""),
            files=list(raw.get("files") or []), assertions=list(raw.get("assertions") or []),
            required=bool(ccl.get("required", False)), known_gap=ccl.get("known_gap"),
            checks=checks, fixtures=fixtures, params=dict(ccl.get("params") or {}),
        ))
    if not cases:
        raise SuiteError(f"{path}: suite has no cases")
    return Suite(name=name, dir=path.parent, target=target, vault=vault, embedder=embedder, cases=cases)


def discover(evals_dir: Path, only: list[str] | None = None) -> list[Suite]:
    suites = [load_suite(p) for p in sorted(evals_dir.glob("*/evals.json"))]
    if only:
        missing = set(only) - {s.name for s in suites}
        if missing:
            raise SuiteError(f"unknown suite(s): {', '.join(sorted(missing))}")
        suites = [s for s in suites if s.name in only]
    if not suites:
        raise SuiteError(f"no suites under {evals_dir}")
    return suites


# ---------- isolated environment ----------

_ENV_CLEAR = ("BRAIN_AUTO_MEMORY", "BRAIN_PROVIDER", "BRAIN_MODEL", "OPS_DB_CONN_REUSE")


class _Patches:
    def __init__(self) -> None:
        self._undo: list[Callable[[], None]] = []

    def setattr(self, obj: Any, name: str, value: Any) -> None:
        old = getattr(obj, name)
        setattr(obj, name, value)
        self._undo.append(lambda: setattr(obj, name, old))

    def setenv(self, name: str, value: str | None) -> None:
        old = os.environ.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

        def undo() -> None:
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        self._undo.append(undo)

    def undo(self) -> None:
        while self._undo:
            self._undo.pop()()


def _copy_vault(src: Path, dst: Path) -> None:
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        out = dst / rel
        if p.is_dir():
            out.mkdir(parents=True, exist_ok=True)
        elif p.suffix == ".md":
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(expand_placeholders(p.read_text(encoding="utf-8")), encoding="utf-8")
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, out)


@contextlib.contextmanager
def case_env(suite: Suite, case: Case, workdir: Path) -> Iterator[_Patches]:
    """Fresh DB + indexed fixture vault + fixtures, all undone on exit."""
    from .. import brain, db, embeddings
    from ..indexers import obsidian

    patches = _Patches()
    try:
        dbfile = workdir / "evals.db"
        patches.setattr(db, "DB_PATH", dbfile)
        patches.setenv("CONDUCTLOOP_DB_PATH", str(dbfile))
        for name in _ENV_CLEAR:
            patches.setenv(name, None)
        patches.setattr(brain, "_PROVIDER_LAST_FAILURE", {})

        mode = case.fixtures.get("embedder", suite.embedder)
        if mode == "stub":
            patches.setattr(embeddings, "embed", stub_embed)
        else:
            def _unavailable(text: str) -> list[float]:
                raise embeddings.EmbeddingsUnavailable("lean build: no local embedding model")
            patches.setattr(embeddings, "embed", _unavailable)

        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            db.init_db()
            embeddings.ensure_schema()
            if suite.vault is not None:
                vault = workdir / "vault"
                _copy_vault(suite.vault, vault)
                obsidian.run(vault)
                embeddings.run()
        for k, v in (case.fixtures.get("memory") or {}).items():
            db.memory_set(str(k), str(v))
        now = int(time.time())
        rev = case.fixtures.get("revenue_events") or []
        if rev:
            with db.db() as conn:
                for i, ev in enumerate(rev):
                    conn.execute(
                        "INSERT INTO revenue_events (source_type, project_key, external_id, event_type,"
                        " amount_cents, currency, status, occurred_at, ingested_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (ev.get("source_type", "stripe"), ev.get("project_key", "demo"),
                         ev.get("external_id", f"evt_eval_{i}"), ev.get("event_type", "payment_succeeded"),
                         int(ev["amount_cents"]), ev.get("currency", "eur"), ev.get("status", "succeeded"),
                         now - int(ev.get("days_ago", 1)) * 86400, now),
                    )
        yield patches
    finally:
        patches.undo()


# ---------- running ----------

@dataclass
class CaseResult:
    suite: str
    key: str
    id: Any
    required: bool
    known_gap: str | None
    passed: bool
    error: str | None
    checks: list[dict]
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite, "key": self.key, "id": self.id, "required": self.required,
            "known_gap": self.known_gap, "passed": self.passed, "error": self.error,
            "checks": self.checks, "warnings": self.warnings, "duration_ms": self.duration_ms,
        }


def cassette_path(suite: Suite, case: Case) -> Path:
    return suite.dir / "cassettes" / f"{case.key}.json"


def _run_target(suite: Suite, case: Case, provider_factory: Callable[[Suite, Case], Any] | None,
                patches: _Patches | None = None) -> tuple[_checks.Outcome, Any]:
    from .. import brain, db

    if suite.target in ("web.ask_page", "browser.agent"):
        from . import web as _web

        if provider_factory:
            web_provider = provider_factory(suite, case)
        elif suite.target == "browser.agent":
            web_provider = _web.ScriptProvider(cassette_path(suite, case))
        else:
            web_provider = _cassette.CassetteProvider(cassette_path(suite, case))
        run_web = _web.run_ask_page if suite.target == "web.ask_page" else _web.run_browser_agent
        return run_web(suite, case, web_provider, patches or _Patches()), web_provider

    if suite.target == "db.search":
        rows = db.search(case.prompt, limit=int(case.params.get("limit", 20)),
                         source=case.params.get("source"), fuzzy=bool(case.params.get("fuzzy", False)))
        return _checks.Outcome(results=[dict(r) for r in rows]), None

    provider = (provider_factory(suite, case) if provider_factory
                else _cassette.CassetteProvider(cassette_path(suite, case)))
    orig = brain.resolve_provider
    brain.resolve_provider = lambda *a, **k: provider  # type: ignore[assignment]
    try:
        ans = brain.ask(case.prompt, k=int(case.params.get("k", 8)))
    finally:
        brain.resolve_provider = orig  # type: ignore[assignment]
    calls = getattr(provider, "calls", [])
    prompt = ""
    if calls:
        first = calls[0]
        prompt = first["system"] + "\n\n" + "\n".join(m["content"] for m in first["messages"])
    return _checks.Outcome(
        answer=ans.answer, citations=list(ans.citations), prompt=prompt,
        memory_proposals=list(ans.memory_proposals), usage=dict(getattr(provider, "last_usage", None) or {}),
    ), provider


def run_case(suite: Suite, case: Case, *, guard: Any = None,
             provider_factory: Callable[[Suite, Case], Any] | None = None) -> CaseResult:
    t0 = time.perf_counter()
    warnings: list[str] = []
    error: str | None = None
    results: list[dict] = []
    before = len(guard.attempts) if guard is not None else 0
    # ignore_cleanup_errors: the case's sqlite file may still be open (a connection
    # not yet garbage-collected); Windows refuses to delete it, and a throwaway
    # directory that cannot be removed must not fail the case.
    with tempfile.TemporaryDirectory(prefix="cl-eval-", ignore_cleanup_errors=True) as td:
        provider: Any = None
        try:
            with case_env(suite, case, Path(td)) as patches:
                outcome, provider = _run_target(suite, case, provider_factory, patches)
            results = [_checks.run_check(outcome, c).as_dict() for c in case.checks]
            perr0 = getattr(provider, "error", None)
            if suite.target == "browser.agent" and isinstance(perr0, _cassette.CassetteError):
                error = f"{perr0.code}: {perr0}"  # the runner swallows provider errors
        except _checks.CheckSpecError:
            raise
        except Exception as e:  # noqa: BLE001 - every failure becomes a failed case
            perr = getattr(provider, "error", None)
            if perr is None and suite.target in ("brain.ask", "web.ask_page") and provider_factory is None:
                # brain.ask wraps provider errors; recover the cassette error if any.
                perr = e.__cause__ if isinstance(e.__cause__, _cassette.CassetteError) else None
            if isinstance(perr, _cassette.CassetteError):
                error = f"{perr.code}: {perr}"
            elif isinstance(e, _cassette.CassetteError):
                error = f"{e.code}: {e}"
            else:
                error = f"exception: {type(e).__name__}: {str(e)[:200]}"
        if provider is not None and getattr(provider, "stale_system", False):
            warnings.append("system prompt changed since this cassette was recorded")
    if guard is not None and len(guard.attempts) > before:
        error = f"network_blocked: {guard.attempts[before]}"
    passed = error is None and all(r["passed"] for r in results)
    return CaseResult(suite=suite.name, key=case.key, id=case.id, required=case.required,
                      known_gap=case.known_gap, passed=passed, error=error, checks=results,
                      warnings=warnings, duration_ms=int((time.perf_counter() - t0) * 1000))


def _score(rs: list[CaseResult]) -> dict[str, Any]:
    n = len(rs)
    p = sum(1 for r in rs if r.passed)
    return {"passed": p, "total": n, "score": round(100.0 * p / n, 2) if n else 0.0}


def run(suites: list[Suite], *, block_network: bool = True,
        provider_factory: Callable[[Suite, Case], Any] | None = None) -> dict[str, Any]:
    cm: contextlib.AbstractContextManager[Any] = (
        _netguard.blocked() if block_network else contextlib.nullcontext(None))
    case_results: list[CaseResult] = []
    with cm as guard:
        for s in suites:
            for c in s.cases:
                case_results.append(run_case(s, c, guard=guard, provider_factory=provider_factory))
    by_suite = {s.name: _score([r for r in case_results if r.suite == s.name]) for s in suites}
    return {
        "version": REPORT_VERSION, "mode": "offline",
        "overall": _score(case_results), "suites": by_suite,
        "cases": [r.as_dict() for r in case_results],
    }


# ---------- baseline ----------

def baseline_from_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": REPORT_VERSION,
        "policy": f"fail if any required case fails or any score drops > {MAX_DROP_POINTS:g} points",
        "overall": report["overall"],
        "suites": report["suites"],
        "cases": {f"{c['suite']}/{c['key']}": c["passed"] for c in report["cases"]},
    }


def compare(report: dict[str, Any], baseline: dict[str, Any] | None,
            max_drop: float = MAX_DROP_POINTS) -> tuple[bool, list[str], list[str]]:
    """Return ``(ok, problems, notes)`` per decision D10."""
    problems: list[str] = []
    notes: list[str] = []
    for c in report["cases"]:
        if c["required"] and not c["passed"]:
            why = c["error"] or "; ".join(x["evidence"] for x in c["checks"] if not x["passed"])
            problems.append(f"required case failed: {c['suite']}/{c['key']}: {why}")
    if baseline is None:
        notes.append("no baseline file: only required cases are enforced")
        return not problems, problems, notes

    def drop(name: str, now: float, ref: float) -> None:
        if ref - now > max_drop + 1e-9:
            problems.append(f"score drop in {name}: {now:.2f} vs baseline {ref:.2f} "
                            f"(-{ref - now:.2f} > {max_drop:g} points)")

    drop("overall", float(report["overall"]["score"]), float(baseline["overall"]["score"]))
    for name, ref in (baseline.get("suites") or {}).items():
        cur = report["suites"].get(name)
        if cur is None:
            problems.append(f"suite in baseline but not run: {name}")
            continue
        drop(f"suite {name}", float(cur["score"]), float(ref["score"]))
    for name in report["suites"]:
        if name not in (baseline.get("suites") or {}):
            notes.append(f"new suite not in baseline yet: {name}")
    base_cases = baseline.get("cases") or {}
    for c in report["cases"]:
        q = f"{c['suite']}/{c['key']}"
        if q in base_cases and not base_cases[q] and c["passed"]:
            notes.append(f"now passing (update baseline): {q}")
        if base_cases.get(q) and not c["passed"] and not c["required"]:
            notes.append(f"regressed (non-required): {q}")
    return not problems, problems, notes


# ---------- reporting ----------

def markdown_summary(report: dict[str, Any], ok: bool, problems: list[str], notes: list[str],
                     baseline: dict[str, Any] | None) -> str:
    lines = [f"## Evals (offline): {'PASS' if ok else 'FAIL'}", ""]
    o = report["overall"]
    ref = f" · baseline {baseline['overall']['score']:.2f}" if baseline else ""
    lines.append(f"**Overall** {o['score']:.2f} ({o['passed']}/{o['total']}){ref}")
    lines += ["", "| Suite | Score | Passed | Baseline |", "|---|---|---|---|"]
    for name, s in report["suites"].items():
        b = (baseline or {}).get("suites", {}).get(name)
        lines.append(f"| {name} | {s['score']:.2f} | {s['passed']}/{s['total']} | "
                     f"{b['score']:.2f} |" if b else f"| {name} | {s['score']:.2f} | {s['passed']}/{s['total']} | — |")
    lines += ["", "| Case | Req | Result | Detail |", "|---|---|---|---|"]
    for c in report["cases"]:
        if c["passed"]:
            detail = ""
        else:
            detail = c["error"] or "; ".join(x["evidence"] for x in c["checks"] if not x["passed"])
            if c["known_gap"]:
                detail = f"known gap: {c['known_gap']} — {detail}"
        detail = detail.replace("|", "\\|")
        lines.append(f"| {c['suite']}/{c['key']} | {'yes' if c['required'] else ''} | "
                     f"{'✅' if c['passed'] else '❌'} | {detail} |")
    if problems:
        lines += ["", "### Problems"] + [f"- {p}" for p in problems]
    if notes:
        lines += ["", "### Notes"] + [f"- {n}" for n in notes]
    warns = [f"{c['suite']}/{c['key']}: {w}" for c in report["cases"] for w in c["warnings"]]
    if warns:
        lines += ["", "### Warnings"] + [f"- {w}" for w in warns]
    return "\n".join(lines) + "\n"
