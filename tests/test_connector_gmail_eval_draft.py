"""Runs docs/evals/connectors-gmail.json today (draft until #28), with the #28
check semantics, against recorded responses and no network."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import test_connector_gmail as tgm
from connectors_native_fake import connect

from ops_dashboard import connector_actions as actions
from ops_dashboard import connector_oauth as oauth
from ops_dashboard import connector_tokens as tokens

SUITE = Path(__file__).resolve().parent.parent / "docs" / "evals" / "connectors-gmail.json"
CASES = json.loads(SUITE.read_text(encoding="utf-8"))["evals"]
_SECRET = re.compile(r"GOCSPX-|fake-at-|fake-rt|\bya29\.|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def run_eval_case(params: dict) -> str:
    fake = tgm.make_fake()
    prev_t, prev_b = oauth.set_transport(fake.transport()), tokens.use_backend(tokens.MemoryBackend())
    try:
        asked = connect(params["connector"], fake, client_secret=tgm.SECRET,
                        permission=params.get("permission", "read"))
        try:
            out = actions.invoke(params["connector"], params["action"], params.get("payload") or {})
        except actions.ActionError as e:
            out = {"ok": False, "error": e.code, "status": e.status}
        if params["action"] == "sync":
            from ops_dashboard import db
            with db.db() as conn:
                out["vault"] = [r[0] for r in conn.execute("SELECT content FROM documents WHERE source='gmail'")]
        out["requested_scopes"] = asked["scope"].split()
        out["pending"] = len(actions.list_actions("pending"))
        out["provider_writes"] = sum(1 for r in fake.requests if r.method != "GET" and r.url.host != "oauth2.googleapis.com")
        return json.dumps(out, ensure_ascii=False, sort_keys=True)
    finally:
        oauth.disconnect(params["connector"])
        tokens.use_backend(prev_b)
        oauth.set_transport(prev_t)


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ev.db")
    db.init_db()


def test_suite_shape():
    keys = [c["conductloop"]["key"] for c in CASES]
    assert len(set(keys)) == len(keys) == 5 and all(c["conductloop"]["required"] for c in CASES)


@pytest.mark.parametrize("case", CASES, ids=[c["conductloop"]["key"] for c in CASES])
def test_eval_case(tmpdb, case):
    answer = run_eval_case(case["conductloop"]["params"])
    for ch in case["conductloop"]["checks"]:
        if ch["type"] == "must_contain":
            assert ch["text"] in answer, (ch, answer)
        elif ch["type"] == "must_not_contain":
            assert ch["text"] not in answer, (ch, answer)
        else:
            assert not _SECRET.search(answer), answer
