"""Linear connector (personal API key): reuses the existing `connections`
row, raw-key Authorization, GraphQL errors mapped, issues ingested as
untrusted text, search, and a comment that waits for confirmation.
Recorded responses, no network."""
from __future__ import annotations

import json

import pytest
from token_fake import Recorded, token_env

pytest.importorskip("cryptography")

from ops_dashboard import connector_actions as actions  # noqa: E402
from ops_dashboard import connector_store as store  # noqa: E402
from ops_dashboard.connectors_token import base, linear  # noqa: E402

KEY = "lin-test-key-not-real"
U1 = "2b7e4c1a-0f3d-4a8e-9c55-1d2e3f4a5b6c"


@pytest.fixture
def lin(tmp_path):
    rec = Recorded.load("linear/recorded.json")
    with token_env(tmp_path, rec) as env:
        base.register_connector(linear.CONNECTOR)
        from ops_dashboard import db
        env["conn_id"] = db.add_connection("linear", "work", {"api_key": KEY})
        yield env


def _docs(db):
    with db.db() as conn:
        return {r[0]: (r[1], r[2], json.loads(r[3])) for r in conn.execute(
            "SELECT path, title, content, metadata FROM documents WHERE source = 'linear'")}


def test_reuses_connection_with_raw_key_header(lin):
    from ops_dashboard import db
    base.connect("linear", connection_id=lin["conn_id"])
    assert store.get_account("linear")["account_label"] == "Ana Example"
    assert lin["rec"].calls[0]["auth"] == KEY                            # no "Bearer"
    assert lin["rec"].calls[0]["body"]["operationName"] == "Viewer"
    assert len(db.list_connections("linear")) == 1 and lin["kc"].items == {}


def test_bad_key_graphql_auth_error(lin):
    from ops_dashboard import db
    with pytest.raises(actions.ActionError) as e:
        base.connect("linear", token="wrong-key")
    assert e.value.code == "unauthorized" and [c["name"] for c in db.list_connections("linear")] == ["work"]


def test_sync_teams_and_assigned(lin):
    from ops_dashboard import db
    base.connect("linear", connection_id=lin["conn_id"])
    assert base.run_sync("linear")["fetched"] == 1                       # no teams → assigned to you
    base.save_config("linear", {"teams": "ENG\nOPS"})
    assert base.run_sync("linear") == {"fetched": 2, "inserted_or_updated": 1, "unchanged": 1}
    docs = _docs(db)
    assert set(docs) == {"linear/issue/ENG-42", "linear/issue/OPS-7"}
    title, content, meta = docs["linear/issue/ENG-42"]
    assert title == "ENG-42 Conectores con token" and f"id: {U1}" in content and "\u2066" not in content
    assert meta["untrusted"] is True and "connectors" in meta["tags"] and "ENG" in meta["tags"]
    body = next(c["body"] for c in lin["rec"].calls if c["body"].get("operationName") == "TeamIssues")
    assert body["variables"] == {"first": 50, "teams": ["ENG", "OPS"]}
    assert all(c["method"] == "POST" and c["body"]["query"].lstrip().startswith("query")
               for c in lin["rec"].calls)                                 # every sync call is a query


def test_config_rejects_bad_team(lin):
    with pytest.raises(actions.ActionError):
        base.save_config("linear", {"teams": "eng; drop"})


def test_search(lin):
    base.connect("linear", connection_id=lin["conn_id"])
    r = actions.invoke("linear", "search_issues", {"term": "conectores"})
    assert r["result"]["untrusted"] is True and r["result"]["items"][0]["identifier"] == "ENG-42"


def test_comment_waits_for_confirmation(lin):
    base.connect("linear", connection_id=lin["conn_id"])
    payload = {"issue_id": U1, "identifier": "ENG-42", "body": "Listo"}
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("linear", "comment_issue", payload)
    assert e.value.code == "write_not_permitted"
    base.set_permission("linear", "write")
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("linear", "comment_issue", {**payload, "issue_id": "ENG-42"})
    assert e.value.code == "bad_issue_id"
    q = actions.invoke("linear", "comment_issue", payload)
    assert q["status"] == "confirmation_required" and "ENG-42" in q["summary"]
    assert U1 in q["summary"] and "el agente dice" in q["summary"]   # the UUID that is really used
    assert not [c for c in lin["rec"].calls if c["body"].get("operationName") == "CommentCreate"]
    done = actions.confirm(q["action_id"], q["payload_hash"])
    assert done["status"] == "done" and done["result"]["id"] == "c-1"
    sent = [c for c in lin["rec"].calls if c["body"].get("operationName") == "CommentCreate"]
    assert len(sent) == 1 and sent[0]["body"]["variables"] == {"issueId": U1, "body": "Listo"}
    assert KEY not in json.dumps(store.list_audit())


def test_ratelimited_graphql_error_maps_to_rate_limited():
    import httpx

    class _C:
        def request(self, *a, **k):
            return httpx.Response(400, json={"errors": [{"message": "Rate limit exceeded",
                                                         "extensions": {"code": "RATELIMITED"}}]})
    with pytest.raises(base.TokenError) as e:
        linear._gql(_C(), "Viewer", linear.Q_VIEWER)
    assert e.value.code == "rate_limited" and e.value.status == 429
