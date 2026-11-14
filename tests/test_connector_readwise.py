"""Readwise connector (access token): 'Token' auth scheme, keychain only,
export pagination, one untrusted vault document per book (discarded
highlights skipped), and a highlight write that waits for confirmation.
Recorded responses, no network."""
from __future__ import annotations

import json

import pytest
from token_fake import Recorded, token_env

pytest.importorskip("cryptography")

from ops_dashboard import connector_actions as actions  # noqa: E402
from ops_dashboard import connector_store as store  # noqa: E402
from ops_dashboard.connectors_token import base, readwise  # noqa: E402

TOKEN = "readwise-test-token-not-real"


@pytest.fixture
def rw(tmp_path):
    with token_env(tmp_path, Recorded.load("readwise/recorded.json")) as env:
        base.register_connector(readwise.CONNECTOR)
        yield env


def _docs(db):
    with db.db() as conn:
        return {r[0]: (r[1], r[2], json.loads(r[3])) for r in conn.execute(
            "SELECT path, title, content, metadata FROM documents WHERE source = 'readwise'")}


def test_token_scheme_and_keychain(rw):
    from ops_dashboard import db
    with pytest.raises(actions.ActionError) as e:
        base.connect("readwise", token="wrong")
    assert e.value.code == "unauthorized" and rw["kc"].items == {}
    base.connect("readwise", token=TOKEN)
    assert rw["rec"].calls[-1]["auth"] == f"Token {TOKEN}"
    assert store.get_account("readwise")["status"] == "connected" and db.list_connections() == []
    assert any(TOKEN in v for v in rw["kc"].items.values())


def test_sync_books_with_highlights(rw):
    from ops_dashboard import db
    base.connect("readwise", token=TOKEN)
    assert base.run_sync("readwise") == {"fetched": 2, "inserted_or_updated": 2, "unchanged": 0, "removed": 0}
    docs = _docs(db)
    assert set(docs) == {"readwise/book/111", "readwise/book/222"}            # 333 only had discarded highlights
    title, content, meta = docs["readwise/book/111"]
    assert title == "Designing Data-Intensive Applications" and "Martin Kleppmann" in content
    assert "> Replication means" in content and "Nota: Clave para el sync" in content
    assert "descartado" not in content and "\u200b" not in content
    assert meta["untrusted"] is True and "databases" in meta["tags"] and meta["url"].startswith("https://readwise.io/")
    assert "Nota: Leer con el equipo" in docs["readwise/book/222"][1]
    assert any("pageCursor=cur-b" in c["url"] for c in rw["rec"].calls) and rw["rec"].writes() == []
    assert base.run_sync("readwise")["unchanged"] == 2


def test_highlight_waits_for_confirmation(rw):
    base.connect("readwise", token=TOKEN)
    payload = {"text": "Los conectores leen primero.", "title": "Notas de ConductLoop", "note": "para el equipo"}
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("readwise", "create_highlight", payload)
    assert e.value.code == "write_not_permitted"
    base.set_permission("readwise", "write")
    for bad, code in (({"text": ""}, "bad_text"), ({"text": "x", "source_url": "http://x"}, "bad_source_url")):
        with pytest.raises(actions.ActionError) as e:
            actions.invoke("readwise", "create_highlight", bad)
        assert e.value.code == code
    q = actions.invoke("readwise", "create_highlight", payload)
    assert q["status"] == "confirmation_required" and "guardar subrayado en «Notas de ConductLoop»" in q["summary"]
    assert rw["rec"].writes() == []
    done = actions.confirm(q["action_id"], q["payload_hash"])
    assert done["result"] == {"books": [{"id": 444, "title": "Notas de ConductLoop"}]}
    w = rw["rec"].writes()
    assert len(w) == 1 and w[0]["body"] == {"highlights": [{**payload, "source_type": "conductloop"}]}
    assert TOKEN not in json.dumps(store.list_audit())
