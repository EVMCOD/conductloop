"""CLI: ``python -m ops_dashboard.evals run|record``."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import record as _record
from . import runner as _runner


def _cmd_run(a: argparse.Namespace) -> int:
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
