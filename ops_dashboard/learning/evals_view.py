"""Read-only view of the evals for the "Aprendizaje" page (PR L7).

The offline harness (``ops_dashboard.evals``, PR #28) is not in this stack, so
this module never imports or runs it. It only *reads* the files the harness
uses — ``evals/<suite>/evals.json`` and ``evals/baseline.json`` — and, once L2
lands, the local ``eval_runs`` table. When the stacks meet, the same view shows
the real suites and baseline with no change here. Nothing is executed and
nothing touches the network.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .. import db

MAX_SUITES = 50
MAX_CASES = 500
MAX_FILE_BYTES = 1024 * 1024
MAX_PROMPT = 200
RUN_COMMAND = "python -m ops_dashboard.evals run"


def evals_dir() -> Path | None:
    """``OPS_EVALS_DIR`` or ``<repo>/evals`` next to the package (source
    checkouts only; the packaged app does not ship the evals)."""
    env = os.environ.get("OPS_EVALS_DIR")
    p = Path(env).expanduser() if env else Path(__file__).resolve().parents[2] / "evals"
    return p if p.is_dir() else None


def harness_installed() -> bool:
    """True when the #28 harness is in this checkout (checked on disk; never imported)."""
    return (Path(__file__).resolve().parents[1] / "evals" / "runner.py").is_file()


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _s(v: Any, n: int = 120) -> str:
    return str(v)[:n] if v is not None else ""


def _suite(path: Path, baseline: dict) -> dict | None:
    data = _read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("evals"), list):
        return None
    meta_raw = data.get("conductloop")
    meta: dict = meta_raw if isinstance(meta_raw, dict) else {}
    name = _s(meta.get("suite") or path.parent.name, 64)
    cases = []
    for c in data["evals"][:MAX_CASES]:
        if not isinstance(c, dict):
            continue
        cl_raw = c.get("conductloop")
        cl: dict = cl_raw if isinstance(cl_raw, dict) else {}
        key = _s(cl.get("key") or c.get("id"), 80)
        checks_raw = cl.get("checks")
        checks: list = checks_raw if isinstance(checks_raw, list) else []
        base = (baseline.get("cases") or {}).get(f"{name}/{key}") if isinstance(baseline.get("cases"), dict) else None
        cases.append({
            "key": key,
            "prompt": _s(c.get("prompt"), MAX_PROMPT),
            "required": cl.get("required") is True,
            "checks": sorted({_s(ch.get("type"), 40) for ch in checks if isinstance(ch, dict) and ch.get("type")}),
            "baseline_passed": base if isinstance(base, bool) else None,
        })
    bsuite = (baseline.get("suites") or {}).get(name) if isinstance(baseline.get("suites"), dict) else None
    return {"suite": name, "target": _s(meta.get("target"), 64), "cases": cases,
            "required": sum(1 for c in cases if c["required"]),
            "baseline": _score(bsuite)}


def _score(d: Any) -> dict | None:
    if not isinstance(d, dict):
        return None
    try:
        return {"passed": int(d.get("passed", 0)), "total": int(d.get("total", 0)),
                "score": float(d.get("score", 0.0))}
    except (TypeError, ValueError):
        return None


def local_runs(limit: int = 20) -> list[dict]:
    """Rows of the L2 ``eval_runs`` table, if that migration exists."""
    with db.db() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='eval_runs'").fetchone():
            return []
        cols = {r[1] for r in conn.execute("PRAGMA table_info(eval_runs)")}
        want = [c for c in ("id", "mode", "suite", "provider", "model", "passed", "failed", "cost_cents_est",
                            "started_at", "finished_at") if c in cols]
        if "id" not in want:
            return []
        q = f"SELECT {', '.join(want)} FROM eval_runs ORDER BY id DESC LIMIT ?"  # noqa: S608 (fixed column names)
        return [dict(r) for r in conn.execute(q, (limit,))]


def overview() -> dict:
    d = evals_dir()
    out: dict[str, Any] = {"available": d is not None, "harness_installed": harness_installed(),
                           "dir": str(d) if d else None, "run_command": RUN_COMMAND, "suites": [],
                           "overall": None, "policy": "", "local_runs": local_runs(), "read_only": True}
    if d is None:
        return out
    baseline = _read_json(d / "baseline.json")
    baseline = baseline if isinstance(baseline, dict) else {}
    out["overall"] = _score(baseline.get("overall"))
    out["policy"] = _s(baseline.get("policy"), 300)
    for f in sorted(d.glob("*/evals.json"))[:MAX_SUITES]:
        s = _suite(f, baseline)
        if s:
            out["suites"].append(s)
    return out
