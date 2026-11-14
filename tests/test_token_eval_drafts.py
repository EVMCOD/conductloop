"""Runs every draft eval suite docs/evals/token-*.json (one per token
connector) with recorded responses and the #28 check semantics
(must_contain, must_not_contain, no_secret). No network."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from token_fake import run_eval_case

pytest.importorskip("cryptography")

EVALS = Path(__file__).resolve().parent.parent / "docs" / "evals"
_SECRET = re.compile(r"sk-ant-|\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}|\bxox[abprs]-|\bgh[pousr]_[A-Za-z0-9]{30,}|"
                     r"\blin_api_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
SUITES = sorted(EVALS.glob("token-*.json"))
CASES = [(p.stem, c) for p in SUITES for c in json.loads(p.read_text(encoding="utf-8"))["evals"]]


@pytest.mark.parametrize("path", SUITES, ids=[p.stem for p in SUITES])
def test_suite_shape(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["conductloop"]["target"] == "connectors.token"
    keys = [c["conductloop"]["key"] for c in data["evals"]]
    assert keys and len(keys) == len(set(keys))
    assert all(re.match(r"^[a-z0-9][a-z0-9\-]{0,63}$", k) for k in keys)
    for c in data["evals"]:
        assert c["prompt"].strip() and c["conductloop"]["checks"] and c["conductloop"]["required"]
        assert {ch["type"] for ch in c["conductloop"]["checks"]} <= {"must_contain", "must_not_contain", "no_secret"}


@pytest.mark.parametrize("suite,case", CASES, ids=[f"{s}:{c['conductloop']['key']}" for s, c in CASES])
def test_eval_case_passes(tmp_path, suite, case):
    answer = run_eval_case(case["conductloop"]["params"], tmp_path)
    for ch in case["conductloop"]["checks"]:
        if ch["type"] == "must_contain":
            assert ch["text"] in answer, (ch, answer)
        elif ch["type"] == "must_not_contain":
            assert ch["text"] not in answer, (ch, answer)
        else:
            assert not _SECRET.search(answer), answer
