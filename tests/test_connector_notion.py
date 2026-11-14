"""Notion connector on the #52 token kit: reuses the existing Notion
connection (no second copy of the token), ingests shared pages as untrusted
text, and never writes. Recorded responses, no network."""
from __future__ import annotations

import json

import pytest
from token_fake import Recorded, token_env

pytest.importorskip("cryptography")

from ops_dashboard import connector_actions as actions  # noqa: E402
from ops_dashboard import connector_store as store  # noqa: E402
from ops_dashboard.connectors_token import base, notion  # noqa: E402

TOKEN = "secret_notion_test_token_not_real"
P1 = "1a2b3c4d-0000-4000-8000-000000000001"


@pytest.fixture
def nt(tmp_path):
    with token_env(tmp_path, Recorded.load("notion/recorded.json")) as env:
        base.register_connector(notion.CONNECTOR)
        yield env


def _docs(db):
    with db.db() as conn:
        return {r[0]: (r[1], r[2], json.loads(r[3])) for r in conn.execute(
            "SELECT path, title, content, metadata FROM documents WHERE source = 'notion'")}


def test_spec_read_only_on_notion_api():
    spec = notion.CONNECTOR.spec
    assert spec.auth == "token" and spec.allowed_hosts == ("api.notion.com",)
    assert {n for n, a in spec.actions.items() if a.kind == "write"} == {"append_to_page", "create_page"}
    assert notion.CONNECTOR.token_mode == "connections" and notion.CONNECTOR.connections_provider == "notion"


def test_reuses_the_existing_conexiones_row(nt):
    from ops_dashboard import db
    cid = db.add_connection("notion", "Mi Notion", {"token": TOKEN})
    v = base.connect("notion", connection_id=cid)
    assert v["linked_connection"] == "Mi Notion" and v["credential_stored"]
    assert [c["id"] for c in db.list_connections("notion")] == [cid]          # not duplicated
    assert nt["kc"].items == {}                                                # nothing in the connector keychain
    assert store.get_account("notion")["account_label"] == "Espacio de Ana"
    assert TOKEN not in base._state_path("notion").read_text()
    call = nt["rec"].calls[0]
    assert call["auth"] == f"Bearer {TOKEN}"


def test_pasted_token_lands_in_connections_not_twice(nt):
    from ops_dashboard import db
    base.connect("notion", token=TOKEN)
    base.connect("notion", token=TOKEN)
    rows = db.list_connections("notion")
    assert len(rows) == 1 and rows[0]["name"] == base.CONNECTIONS_NAME and nt["kc"].items == {}


def test_bad_token_stores_nothing(nt):
    from ops_dashboard import db
    with pytest.raises(actions.ActionError) as e:
        base.connect("notion", token="secret_wrong")
    assert e.value.code == "unauthorized" and db.list_connections("notion") == []


def test_sync_ingests_pages_as_untrusted_text(nt):
    from ops_dashboard import db
    base.connect("notion", token=TOKEN)
    assert base.run_sync("notion") == {"fetched": 2, "inserted_or_updated": 2, "unchanged": 0}
    assert base.run_sync("notion")["unchanged"] == 2
    docs = _docs(db)
    title, content, meta = docs[f"notion/page/{P1}"]
    assert title == "Plan Q4" and meta["untrusted"] is True and meta["url"].startswith("https://www.notion.so/")
    assert "## Objetivos" in content and "- [x] Lanzar conectores" in content
    assert "▸ Detalles\n  - Slack y M365" in content and "[image] diagrama" in content
    assert "\u200b" not in content and "s3.example" not in content          # files never fetched
    meeting = docs["notion/page/1a2b3c4d-0000-4000-8000-000000000003"][1]
    assert "Asistentes: Ana, Luis" in meeting and "1. Revisar #62" in meeting
    assert not any("000000000002" in k for k in docs)                        # archived page skipped
    assert nt["rec"].writes() == [] and actions.list_actions("pending") == []
    assert all(c["url"].startswith("https://api.notion.com/") for c in nt["rec"].calls)


def test_search_and_get_page(nt):
    base.connect("notion", token=TOKEN)
    res = actions.invoke("notion", "search", {"query": "plan"})["result"]
    assert res["pages"][0]["title"] == "Plan Q4"
    page = actions.invoke("notion", "get_page", {"page_id": P1})["result"]
    assert page["text"].startswith("## Objetivos")
    with pytest.raises(actions.ActionError):
        actions.invoke("notion", "get_page", {"page_id": "../users"})
    assert nt["rec"].writes() == []


def test_writes_off_until_allowed(nt):
    base.connect("notion", token=TOKEN)
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("notion", "append_to_page", {"page_id": P1, "text": "hola"})
    assert e.value.code == "write_not_permitted" and nt["rec"].writes() == []
