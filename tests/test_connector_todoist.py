"""Todoist connector (personal token, API v1): token only in the keychain,
cursor pagination, tasks ingested as untrusted text, project filter, and
create/close writes that wait for confirmation. Recorded responses, no
network."""
from __future__ import annotations

import json

import pytest
from token_fake import Recorded, token_env

pytest.importorskip("cryptography")

from ops_dashboard import connector_actions as actions  # noqa: E402
from ops_dashboard import connector_store as store  # noqa: E402
from ops_dashboard.connectors_token import base, todoist  # noqa: E402

TOKEN = "todoist-test-token-not-real"


@pytest.fixture
def td(tmp_path):
    with token_env(tmp_path, Recorded.load("todoist/recorded.json")) as env:
        base.register_connector(todoist.CONNECTOR)
        yield env


def _docs(db):
    with db.db() as conn:
        return {r[0]: (r[1], r[2], json.loads(r[3])) for r in conn.execute(
            "SELECT path, title, content, metadata FROM documents WHERE source = 'todoist'")}


def test_token_only_in_keychain(td):
    from ops_dashboard import db
    with pytest.raises(actions.ActionError) as e:
        base.connect("todoist", token="wrong")
    assert e.value.code == "unauthorized" and td["kc"].items == {}
    v = base.connect("todoist", token=TOKEN)
    assert v["credential_stored"] and store.get_account("todoist")["account_label"] == "Ana Example"
    assert db.list_connections() == [] and any(TOKEN in x for x in td["kc"].items.values())
    assert TOKEN not in base._state_path("todoist").read_text()


def test_sync_paginates_and_ingests(td):
    from ops_dashboard import db
    base.connect("todoist", token=TOKEN)
    assert base.run_sync("todoist") == {"fetched": 2, "inserted_or_updated": 2, "unchanged": 0, "removed": 0}
    docs = _docs(db)
    title, content, meta = docs["todoist/task/6X7rfFVPjhvv84XG"]
    assert title == "Revisar PR #52" and "ConductLoop · p1 · vence mañana · review" in content
    assert "\u200b" not in content and meta["untrusted"] is True
    assert meta["url"] == "https://app.todoist.com/app/task/6X7rfFVPjhvv84XG" and "ConductLoop" in meta["tags"]
    assert "todoist/task/6X7rfEVP8hvv25ZQ" in docs and td["rec"].writes() == []
    assert any("cursor=cur-2" in c["url"] for c in td["rec"].calls)


def test_project_filter(td):
    from ops_dashboard import db
    base.connect("todoist", token=TOKEN)
    base.save_config("todoist", {"projects": "conductloop"})
    assert base.run_sync("todoist")["fetched"] == 1 and list(_docs(db)) == ["todoist/task/6X7rfFVPjhvv84XG"]
    base.save_config("todoist", {"projects": "No existe"})
    with pytest.raises(actions.ActionError) as e:
        base.run_sync("todoist")
    assert e.value.code == "unknown_project"


def test_create_and_close_wait_for_confirmation(td):
    base.connect("todoist", token=TOKEN)
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("todoist", "create_task", {"content": "x"})
    assert e.value.code == "write_not_permitted"
    base.set_permission("todoist", "write")
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("todoist", "create_task", {"content": ""})
    assert e.value.code == "bad_content"
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("todoist", "close_task", {"task_id": "../../x"})
    assert e.value.code == "bad_task_id"
    q1 = actions.invoke("todoist", "create_task", {"content": "Probar Linear en el Mac", "due_string": "mañana",
                                                    "project": "ConductLoop"})
    q2 = actions.invoke("todoist", "close_task", {"task_id": "6X7rfFVPjhvv84XG", "content": "Revisar PR #52"})
    assert "crear tarea «Probar Linear en el Mac» en ConductLoop (vence mañana)" in q1["summary"]
    # The real target (task_id) is always shown; the caller's title is only a claim.
    assert "completar la tarea " in q2["summary"] and "«Revisar PR #52»" in q2["summary"]
    assert "el agente dice" in q2["summary"] and td["rec"].writes() == []
    assert actions.confirm(q1["action_id"], q1["payload_hash"])["result"]["id"] == "6X7rgg8Q4Fh2vr3m"
    assert actions.confirm(q2["action_id"], q2["payload_hash"])["result"] == {"closed": "6X7rfFVPjhvv84XG"}
    w = td["rec"].writes()
    assert [c["url"] for c in w] == ["https://api.todoist.com/api/v1/tasks",
                                     "https://api.todoist.com/api/v1/tasks/6X7rfFVPjhvv84XG/close"]
    assert w[0]["body"] == {"content": "Probar Linear en el Mac", "due_string": "mañana",
                            "project_id": "6X7rM8997g3RQmvh"}
    assert all(c["auth"] == f"Bearer {TOKEN}" for c in w)
    assert TOKEN not in json.dumps(store.list_audit())


def test_disconnect_deletes_keychain_token(td):
    from ops_dashboard import connector_oauth as oauth
    base.connect("todoist", token=TOKEN)
    oauth.disconnect("todoist")
    assert td["kc"].items == {} and store.get_account("todoist")["status"] == "disconnected"
