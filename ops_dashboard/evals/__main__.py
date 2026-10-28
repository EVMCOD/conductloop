"""CLI: ``python -m ops_dashboard.evals run [--live …]|record``."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from pathlib import Path

from . import live as _live
from . import record as _record
from . import runner as _runner


def _cmd_run(a: argparse.Namespace) -> int:
    if a.live or _live.opt_in_env():
        return _cmd_run_live(a)
    stray = [f for f in ("provider", "model", "key_ref", "budget_eur", "max_calls", "max_output_tokens")
             if getattr(a, f) is not None] + [f for f in ("record", "judge") if getattr(a, f)]
    if stray:
        sys.stderr.write(f"--{stray[0].replace('_', '-')} only applies to live evals (add --live)\n")
        return 2
    evals_dir = Path(a.evals_dir) if a.evals_dir else _runner.default_evals_dir()
    suites = _runner.discover(evals_dir, a.suite or None)
    report = _runner.run(suites)
    bpath = Path(a.baseline) if a.baseline else evals_dir / "baseline.json"
    baseline = json.loads(bpath.read_text(encoding="utf-8")) if bpath.is_file() else None
    if a.suite and baseline:
        baseline = {**baseline, "suites": {k: v for k, v in baseline["suites"].items() if k in a.suite},
                    "overall": report["overall"]}
    ok, problems, notes = _runner.compare(report, baseline)
    md = _runner.markdown_summary(report, ok, problems, notes, baseline)
    sys.stdout.write(md)
    if a.report:
        Path(a.report).write_text(json.dumps({**report, "ok": ok, "problems": problems, "notes": notes},
                                             ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = a.summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(md)
    if a.update_baseline:
        if _record.ci_marker():
            sys.stderr.write("refusing to rewrite the baseline in CI\n")
            return 2
        if a.suite:
            sys.stderr.write("--update-baseline needs a full run (no --suite)\n")
            return 2
        if any(c["required"] and not c["passed"] for c in report["cases"]):
            sys.stderr.write("refusing to write a baseline with failing required cases\n")
            return 1
        bpath.write_text(json.dumps(_runner.baseline_from_report(report), ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
        sys.stdout.write(f"baseline written: {bpath}\n")
        return 0
    return 0 if ok else 1


def _cmd_run_live(a: argparse.Namespace) -> int:
    """Live evals with the user's own key (L2). Never in CI; see live.py."""
    try:
        _live.refuse_in_ci()
        if a.update_baseline:
            raise _live.LiveRefused("baseline_is_offline", "--update-baseline only applies to offline runs")
        record_dir = None
        if a.record:
            record_dir = Path(a.record_dir).expanduser() if a.record_dir else _live.default_record_dir()
        cfg = _live.validate_config(provider=a.provider, model=a.model, key_ref=a.key_ref,
                                    budget_eur=a.budget_eur, max_calls=a.max_calls,
                                    max_output_tokens=a.max_output_tokens, record_dir=record_dir,
                                    judge=a.judge)
        evals_dir = Path(a.evals_dir) if a.evals_dir else _runner.default_evals_dir()
        if evals_dir.resolve() != _runner.default_evals_dir().resolve():
            sys.stderr.write("warning: custom --evals-dir: every note in its fixture vaults is sent to "
                             f"{cfg.provider}; do not point it at personal notes you would not share\n")
        suites = _runner.discover(evals_dir, a.suite or None)
        bpath = Path(a.baseline) if a.baseline else evals_dir / "baseline.json"
        baseline = json.loads(bpath.read_text(encoding="utf-8")) if bpath.is_file() else None
        from .. import db
        with contextlib.redirect_stdout(io.StringIO()):
            db.init_db()
        report_dir = Path(a.out_dir).expanduser() if a.out_dir else _live.default_report_dir()
        report = _live.run_live(suites, cfg, baseline=baseline, report_dir=report_dir)
    except _live.LiveRefused as e:
        sys.stderr.write(f"live evals refused ({e.code}): {e}\n")
        return 3
    sys.stdout.write(_live.redact(_live.markdown_summary(report, baseline)))
    sys.stdout.write(f"\nreport: {report.get('report_path')}\n")
    if a.report:
        Path(a.report).write_text(_live.redact(json.dumps(report, ensure_ascii=False, indent=2)) + "\n",
                                  encoding="utf-8")
    if not report["complete"]:
        return 4
    return 0 if report["ok"] else 1


def _cmd_record(a: argparse.Namespace) -> int:
    try:
        _record.refuse_in_ci()
        evals_dir = Path(a.evals_dir) if a.evals_dir else _runner.default_evals_dir()
        suite = _runner.discover(evals_dir, [a.suite])[0]
        case = next((c for c in suite.cases if c.key == a.case), None)
        if case is None:
            sys.stderr.write(f"unknown case {a.case!r} in suite {a.suite}\n")
            return 2
        out = _record.record_case(suite, case, provider=a.provider, model=a.model, key_name=a.key_name,
                                  out_dir=Path(a.out_dir) if a.out_dir else _record.default_out_dir())
    except _record.RecordRefused as e:
        sys.stderr.write(f"record refused ({e.code}): {e}\n")
        return 3
    sys.stdout.write(f"cassette written (review before copying into the repo): {out}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m ops_dashboard.evals")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run offline evals (no network, no keys)")
    r.add_argument("--evals-dir")
    r.add_argument("--suite", action="append")
    r.add_argument("--baseline")
    r.add_argument("--report", help="write the JSON report here")
    r.add_argument("--summary", help="append the Markdown summary here (default: $GITHUB_STEP_SUMMARY)")
    r.add_argument("--update-baseline", action="store_true")
    lv = r.add_argument_group("live (L2): your own key, your own machine, never CI")
    lv.add_argument("--live", action="store_true",
                    help=f"run against a real provider (or set {_live.OPT_IN_ENV}=1)")
    lv.add_argument("--provider", help=f"one of {', '.join(_live.LIVE_PROVIDERS)} (no fallback)")
    lv.add_argument("--model", help="model id; must have a price in Sesiones → Precios (except ollama)")
    lv.add_argument("--key-ref", help="keychain item name, e.g. byok.anthropic.personal (never an env var)")
    lv.add_argument("--budget-eur", type=float, help=f"mandatory spending cap in EUR (max {_live.MAX_BUDGET_EUR:g})")
    lv.add_argument("--max-calls", type=int, help=f"mandatory cap on provider calls (max {_live.MAX_CALLS_LIMIT})")
    lv.add_argument("--max-output-tokens", type=int,
                    help=f"per-call output cap (default {_live.DEFAULT_MAX_OUTPUT_TOKENS})")
    lv.add_argument("--record", action="store_true", help="write redacted cassettes to a local dir (not the repo)")
    lv.add_argument("--record-dir", help="where --record writes (default: <data dir>/evals/recordings/)")
    lv.add_argument("--judge", action="store_true", help="grade natural-language assertions (same budget)")
    lv.add_argument("--out-dir", help="report dir (default: <data dir>/evals/live/)")
    r.set_defaults(fn=_cmd_run)
    rec = sub.add_parser("record", help="maintainer-only: re-record one cassette with your own key")
    rec.add_argument("--evals-dir")
    rec.add_argument("--suite", required=True)
    rec.add_argument("--case", required=True)
    rec.add_argument("--provider", required=True, choices=_record.PROVIDERS)
    rec.add_argument("--model")
    rec.add_argument("--key-name", help="name in the local secret store (never an env var)")
    rec.add_argument("--out-dir")
    rec.set_defaults(fn=_cmd_record)
    a = p.parse_args(argv)
    try:
        return int(a.fn(a))
    except _runner.SuiteError as e:
        sys.stderr.write(f"suite error: {e}\n")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
