"""GitHub connector (personal token): reuses the existing `connections` row,
ingests issues/PRs into the vault as untrusted text, search, and a comment
write that waits for confirmation. Recorded responses, no network."""
from __future__ import annotations

import json

import pytest
from token_fake import Recorded, token_env

pytest.importorskip("cryptography")

from ops_dashboard import connector_actions as actions  # noqa: E402
from ops_dashboard import connector_store as store  # noqa: E402
from ops_dashboard.connectors_token import (
    base,  # noqa: E402
    github,  # noqa: E402
)

TOKEN = "test-gh-token-not-real"


@pytest.fixture
def gh(tmp_path):
    rec = Recorded.load("github/recorded.json")
    with token_env(tmp_path, rec) as env:
        base.register_connector(github.CONNECTOR)
        from ops_dashboard import db
        env["conn_id"] = db.add_connection("github", "personal", {"token": TOKEN})
        yield env


def _docs(db):
    with db.db() as conn:
        return {r[0]: (r[1], r[2], json.loads(r[3])) for r in conn.execute(
            "SELECT path, title, content, metadata FROM documents WHERE source = 'github'")}


def test_spec_and_reuse_of_existing_connection(gh):
    from ops_dashboard import db
    spec = github.CONNECTOR.spec
    assert spec.auth == "token" and spec.allowed_hosts == ("api.github.com",)
    assert {a.name: a.kind for a in spec.actions.values()} == {"sync": "read", "search_issues": "read",
                                                              "comment_issue": "write"}
    v = base.connect("github", connection_id=gh["conn_id"])
    assert v["linked_connection"] == "personal" and store.get_account("github")["account_label"] == "example-user"
    assert len(db.list_connections("github")) == 1 and gh["kc"].items == {}
    assert gh["rec"].calls[0]["auth"] == f"Bearer {TOKEN}"


def test_bad_token_is_not_saved(gh):
    from ops_dashboard import db
    with pytest.raises(actions.ActionError) as e:
        base.connect("github", token="wrong-token")
    assert e.value.code == "unauthorized" and [c["name"] for c in db.list_connections("github")] == ["personal"]


def test_sync_configured_repos(gh):
    from ops_dashboard import db
    base.connect("github", connection_id=gh["conn_id"])
    base.save_config("github", {"repos": "example-org/example-repo"})
    assert actions.invoke("github", "sync", {})["result"] == {"fetched": 3, "inserted_or_updated": 3, "unchanged": 0}
    docs = _docs(db)
    assert set(docs) == {"github/pr/example-org_example-repo#40", "github/issue/example-org_example-repo#12",
                         "github/issue/example-org_example-repo#13"}
    title, content, meta = docs["github/issue/example-org_example-repo#12"]
    assert title == "example-org/example-repo#12 Search ignores accents"
    assert "closed" in content and "bug, search" in content and "\u200b" not in content
    assert meta["untrusted"] is True and meta["url"] == "https://github.com/example-org/example-repo/issues/12"
    assert "search" in meta["tags"] and gh["rec"].writes() == []
    assert base.run_sync("github")["unchanged"] == 3
    assert [dict(r)["path"] for r in db.search("accents", source="github")] == ["github/issue/example-org_example-repo#12"]


def test_sync_assigned_when_no_repos(gh):
    from ops_dashboard import db
    base.connect("github", connection_id=gh["conn_id"])
    assert base.run_sync("github")["fetched"] == 1
    assert list(_docs(db)) == ["github/issue/example-org_example-site#7"]


def test_config_rejects_bad_repos(gh):
    with pytest.raises(actions.ActionError) as e:
        base.save_config("github", {"repos": "example-org/example-repo\n../../etc"})
    assert e.value.code == "bad_config"


def test_search_is_read_and_untrusted(gh):
    base.connect("github", connection_id=gh["conn_id"])
    r = actions.invoke("github", "search_issues", {"q": "repo:example-org/example-repo is:open accents"})
    assert r["result"]["untrusted"] is True and r["result"]["items"][0]["number"] == 12
    with pytest.raises(actions.ActionError):
        actions.invoke("github", "search_issues", {"q": ""})


def test_comment_waits_for_confirmation(gh):
    base.connect("github", connection_id=gh["conn_id"])
    payload = {"repo": "example-org/example-repo", "number": 12, "body": "Arreglado en #31"}
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("github", "comment_issue", payload)
    assert e.value.code == "write_not_permitted"
    base.set_permission("github", "write")
    with pytest.raises(actions.ActionError) as e:                      # bad payload rejected before queueing
        actions.invoke("github", "comment_issue", {**payload, "repo": "nope"})
    assert e.value.code == "bad_repo"
    q = actions.invoke("github", "comment_issue", payload)
    assert q["status"] == "confirmation_required" and "example-org/example-repo#12" in q["summary"]
    assert gh["rec"].writes() == []
    done = actions.confirm(q["action_id"], q["payload_hash"])
    assert done["status"] == "done" and done["result"]["id"] == 9001
    w = gh["rec"].writes()
    assert len(w) == 1 and w[0]["body"] == {"body": "Arreglado en #31"} and w[0]["auth"] == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(store.list_audit())


def test_revoked_token_marks_error(gh):
    base.connect("github", connection_id=gh["conn_id"])
    base.save_config("github", {"repos": "example-org/example-repo"})
    gh["rec"].entries.insert(0, {"method": "GET", "status": 401, "json": {"message": "Bad credentials"},
                                 "url": "https://api.github.com/repos/example-org/example-repo/issues?state=all&sort=updated&direction=desc&per_page=50"})
    with pytest.raises(actions.ActionError):
        base.run_sync("github")
    assert store.get_account("github")["status"] == "error"
