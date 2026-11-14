"""Notion additive writes (append_to_page, create_page): off until allowed,
queued until the exact payload is confirmed, validated before queueing.
Recorded responses, no network."""
from __future__ import annotations

import pytest
from token_fake import Recorded, token_env

pytest.importorskip("cryptography")

from ops_dashboard import connector_actions as actions  # noqa: E402
from ops_dashboard.connectors_token import base, notion  # noqa: E402

TOKEN = "secret_notion_test_token_not_real"
P1 = "1a2b3c4d-0000-4000-8000-000000000001"


@pytest.fixture
def nt(tmp_path):
    with token_env(tmp_path, Recorded.load("notion/recorded.json")) as env:
        base.register_connector(notion.CONNECTOR)
        base.connect("notion", token=TOKEN)
        base.set_permission("notion", "write")
        yield env


def test_append_pending_then_exact_blocks_once(nt):
    q = actions.invoke("notion", "append_to_page", {"page_id": P1, "text": "Primera idea.\n\nSegunda\u200b idea."})
    assert q["status"] == "confirmation_required" and nt["rec"].writes() == []
    assert q["summary"] == f"Notion · AÑADIR al final de la página {P1} · 2 párrafo(s): Primera idea. Segunda idea."
    with pytest.raises(actions.ActionError):
        actions.confirm(q["action_id"], "1" * 64)
    assert nt["rec"].writes() == []
    assert actions.confirm(q["action_id"], q["payload_hash"])["result"] == {"page_id": P1, "added": 2}
    (w,) = nt["rec"].writes()
    assert w["method"] == "PATCH" and w["url"].endswith(f"/blocks/{P1}/children")
    texts = [b["paragraph"]["rich_text"][0]["text"]["content"] for b in w["body"]["children"]]
    assert texts == ["Primera idea.", "Segunda idea."]                 # invisible chars stripped
    with pytest.raises(actions.ActionError):
        actions.confirm(q["action_id"], q["payload_hash"])
    assert len(nt["rec"].writes()) == 1


def test_create_page_under_parent(nt):
    q = actions.invoke("notion", "create_page", {"parent_page_id": P1, "title": "Acta 21-10", "text": "Asistentes: Ana"})
    assert q["summary"].startswith(f"Notion · CREAR página «Acta 21-10» dentro de {P1}")
    res = actions.confirm(q["action_id"], q["payload_hash"])["result"]
    assert res["url"].startswith("https://www.notion.so/")
    (w,) = nt["rec"].writes()
    assert w["body"]["parent"] == {"page_id": P1}
    assert w["body"]["properties"]["title"]["title"][0]["text"]["content"] == "Acta 21-10"
    assert w["body"]["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == "Asistentes: Ana"


def test_long_text_is_chunked_within_notion_limits(nt):
    q = actions.invoke("notion", "append_to_page", {"page_id": P1, "text": "a" * 4500})
    assert "3 párrafo(s)" in q["summary"]


@pytest.mark.parametrize("action,bad", [
    ("append_to_page", {"page_id": "../users/me", "text": "x"}),
    ("append_to_page", {"page_id": P1, "text": ""}),
    ("append_to_page", {"page_id": P1, "text": "x" * 20001}),
    ("append_to_page", {"page_id": P1, "text": "x", "children": [{"type": "embed"}]}),
    ("append_to_page", {"page_id": P1, "text": "\n\n".join(["p"] * 101)}),
    ("create_page", {"parent_page_id": P1, "title": ""}),
    ("create_page", {"parent_page_id": P1, "title": "a\nb"}),
    ("create_page", {"parent_page_id": P1, "title": "ok", "properties": {"Estado": "Hecho"}}),
    ("create_page", {"parent_database_id": P1, "title": "ok"}),
])
def test_bad_writes_refused_before_queueing(nt, action, bad):
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("notion", action, bad)
    assert e.value.code in ("bad_payload", "bad_page_id")
    assert actions.list_actions("pending") == [] and nt["rec"].writes() == []


def test_back_to_read_expires_pending_writes(nt):
    q = actions.invoke("notion", "append_to_page", {"page_id": P1, "text": "hola"})
    base.set_permission("notion", "read")
    with pytest.raises(actions.ActionError):
        actions.confirm(q["action_id"], q["payload_hash"])
    assert nt["rec"].writes() == []


def test_no_destructive_actions():
    names = set(notion.CONNECTOR.spec.actions)
    assert not names & {"delete_page", "archive_page", "update_page", "update_block", "delete_block"}
