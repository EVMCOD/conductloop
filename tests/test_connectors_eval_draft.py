"""Runs the draft eval suite docs/evals/connectors-write-guard.json today,
with the same check semantics the #28 harness uses (must_contain,
must_not_contain, no_secret), so the case is proven before #28 merges."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from connectors_fake import run_eval_case

SUITE = Path(__file__).resolve().parent.parent / "docs" / "evals" / "connectors-write-guard.json"
# Same families as ops_dashboard/evals/checks.py (#28) for the no_secret check.
_SECRET = re.compile(r"sk-ant-|\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}|\bxox[abprs]-|\bgh[pousr]_[A-Za-z0-9]{30,}|"
                     r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
CASES = json.loads(SUITE.read_text(encoding="utf-8"))["evals"]


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "evals.db")
    db.init_db()


def test_suite_shape_matches_harness_format():
    data = json.loads(SUITE.read_text(encoding="utf-8"))
    assert data["conductloop"]["target"] == "connectors.invoke"
    keys = [c["conductloop"]["key"] for c in CASES]
    assert len(keys) == len(set(keys)) == 3
    assert all(re.match(r"^[a-z0-9][a-z0-9\-]{0,63}$", k) for k in keys)
    for c in CASES:
        assert c["prompt"].strip() and c["conductloop"]["checks"] and c["conductloop"]["required"]
        assert {ch["type"] for ch in c["conductloop"]["checks"]} <= {"must_contain", "must_not_contain", "no_secret"}


@pytest.mark.parametrize("case", CASES, ids=[c["conductloop"]["key"] for c in CASES])
def test_eval_case_passes(tmpdb, case):
    answer = run_eval_case(case["conductloop"]["params"])
    for ch in case["conductloop"]["checks"]:
        if ch["type"] == "must_contain":
            assert ch["text"] in answer, (ch, answer)
        elif ch["type"] == "must_not_contain":
            assert ch["text"] not in answer, (ch, answer)
        elif ch["type"] == "no_secret":
            assert not _SECRET.search(answer)
