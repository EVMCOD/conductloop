"""Kit for token / no-auth connectors (connectors_token.base):
credentials verified before they are stored, reuse of `connections`,
read-only ingest into the vault, host allow-list, confirm-before-write,
routes behind the connectors guard and the page script rules. No network."""
from __future__ import annotations

import json
import re
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from posix_perms import assert_mode
from token_fake import FAKE_TOKEN, Recorded, fake_recorded, make_fake, token_env

pytest.importorskip("cryptography")

from ops_dashboard import connector_actions as actions  # noqa: E402
from ops_dashboard import connector_store as store  # noqa: E402
from ops_dashboard.connectors_token import base  # noqa: E402

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"


@pytest.fixture
def env(tmp_path):
    rec = fake_recorded()
    with token_env(tmp_path, rec) as e:
        yield e
        for cid in ("fakenotes", "fakegh"):
            base.unregister_connector(cid)


def _docs():
    from ops_dashboard import db
    with db.db() as conn:
        return [dict(r) for r in conn.execute("SELECT path, title, content, metadata FROM documents "
                                              "WHERE source = 'fakenotes' ORDER BY path")]


def test_keychain_token_verified_before_storing(env):
    make_fake("keychain")
    with pytest.raises(actions.ActionError) as e:
        base.connect("fakenotes", token="wrong-token")
    assert e.value.code == "unauthorized" and env["kc"].items == {}
    assert store.get_account("fakenotes")["status"] == "disconnected"
    with pytest.raises(actions.ActionError) as e:
        base.connect("fakenotes", token="has space")
    assert e.value.code == "bad_token"
    v = base.connect("fakenotes", token=FAKE_TOKEN)
    acc = store.get_account("fakenotes")
    assert acc["status"] == "connected" and acc["permission"] == "read" and acc["account_label"] == "ana"
    assert v["credential_stored"] is True and FAKE_TOKEN not in json.dumps(v)
    assert any(FAKE_TOKEN in val for val in env["kc"].items.values())
    events = [a["event"] for a in store.list_audit(connector_id="fakenotes")]
    assert "connect_failed" in events and "connected" in events
    assert FAKE_TOKEN not in json.dumps(store.list_audit())


def test_connections_mode_reuses_existing_rows(env):
    from ops_dashboard import db
    make_fake("connections", cid="fakegh", provider="github")
    other = db.add_connection("linear", "work", {"api_key": "lin_api_x"})
    mine = db.add_connection("github", "personal", {"token": FAKE_TOKEN})
    with pytest.raises(actions.ActionError) as e:
        base.connect("fakegh", connection_id=other)                     # wrong provider
    assert e.value.code == "unknown_connection"
    v = base.connect("fakegh", connection_id=mine)
    assert v["linked_connection"] == "personal" and len(db.list_connections("github")) == 1
    assert env["kc"].items == {}                                         # nothing duplicated in the keychain
    base.connect("fakegh", token=FAKE_TOKEN)                             # paste → one 'conectores' row
    base.connect("fakegh", token=FAKE_TOKEN)                             # again → same row updated
    names = sorted(c["name"] for c in db.list_connections("github"))
    assert names == ["conectores", "personal"]
    with db.db() as conn:
        raw = conn.execute("SELECT credentials FROM connections WHERE name='conectores'").fetchone()[0]
    assert raw.startswith("enc:") and FAKE_TOKEN not in raw
    db.delete_connection(db.get_connection_by_name("github", "conectores")["id"])
    with pytest.raises(actions.ActionError) as e:                        # row deleted in "Conexiones"
        base.run_sync("fakegh")
    assert e.value.code in ("not_connected", "action_failed")
    assert store.get_account("fakegh")["status"] == "error"


def test_sync_ingests_untrusted_clean_text_and_forget(env):
    make_fake("keychain")
    base.connect("fakenotes", token=FAKE_TOKEN)
    r = actions.invoke("fakenotes", "sync", {})
    assert r["result"] == {"fetched": 2, "inserted_or_updated": 2, "unchanged": 0}
    docs = _docs()
    assert [d["path"] for d in docs] == ["fakenotes/note/1", "fakenotes/note/2"]
    assert docs[0]["title"] == "Plan Q3" and "\u202e" not in docs[0]["content"]
    meta = json.loads(docs[0]["metadata"])
    assert meta["untrusted"] is True and meta["connector"] == "fakenotes" and "fake" in meta["tags"]
    assert base.run_sync("fakenotes") == {"fetched": 2, "inserted_or_updated": 0, "unchanged": 2}
    assert base.view("fakenotes")["last_sync"]["fetched"] == 2 and base.view("fakenotes")["documents"] == 2
    assert env["rec"].writes() == []                                     # sync never writes upstream
    assert base.forget("fakenotes") == 2 and _docs() == []
    ev = [a["event"] for a in store.list_audit(connector_id="fakenotes")]
    assert {"synced", "data_forgotten", "read"} <= set(ev)


def test_search_finds_ingested_items(env):
    from ops_dashboard import db
    make_fake("keychain")
    base.connect("fakenotes", token=FAKE_TOKEN)
    base.run_sync("fakenotes")
    assert [dict(h)["path"] for h in db.search("octubre", source="fakenotes")] == ["fakenotes/note/1"]


def test_client_refuses_other_hosts_http_redirects_and_big_bodies(env):
    make_fake("keychain")
    base.connect("fakenotes", token=FAKE_TOKEN)
    for url, code in (("https://evil.test/x", "host_not_allowed"), ("http://api.notes.fake.test/notes", "host_not_allowed")):
        with pytest.raises(actions.ActionError) as e:
            actions.invoke("fakenotes", "fetch", {"url": url})
        assert e.value.code == "action_failed" and str(e.value) == code
    env["rec"].entries.insert(0, {"method": "GET", "url": "https://api.notes.fake.test/redir", "status": 302,
                                  "headers": {"Location": "https://evil.test/"}, "text": ""})
    env["rec"].entries.insert(0, {"method": "GET", "url": "https://api.notes.fake.test/big", "text": "x" * (base.MAX_RESPONSE + 10)})
    for path, code in (("redir", "redirect_refused"), ("big", "response_too_large")):
        with pytest.raises(actions.ActionError) as e:
            actions.invoke("fakenotes", "fetch", {"url": f"https://api.notes.fake.test/{path}"})
        assert str(e.value) == code
    assert all("evil.test" not in c["url"] for c in env["rec"].calls)


def test_user_hosts_must_resolve_public(tmp_path):
    from ops_dashboard import connector_registry as registry
    spec = registry.ConnectorSpec(id="fakefeed", name="Feed", auth="token", allowed_hosts=("user-configured.invalid",))
    nc = base.TokenConnector(spec=spec, token_mode="none", whoami=lambda c: "", sync=lambda c, cfg: [])
    for addrs, ok in ((["10.0.0.5"], False), (["127.0.0.1"], False), (["169.254.169.254"], False),
                      (["::1"], False), (["fd00::1"], False), (["93.184.216.34"], True)):
        rec = Recorded([{"method": "GET", "url": "https://feeds.example.org/rss", "text": "<rss/>"}])
        with token_env(tmp_path, rec, resolver=lambda h, a=addrs: a):
            c = base.TokenClient(nc, "", ("feeds.example.org",))
            if ok:
                assert c.get_text("https://feeds.example.org/rss") == "<rss/>"
            else:
                with pytest.raises(actions.ActionError) as e:
                    c.get_text("https://feeds.example.org/rss")
                assert e.value.code == "host_not_public" and rec.calls == []


def test_writes_need_permission_and_confirmation(env):
    make_fake("keychain")
    base.connect("fakenotes", token=FAKE_TOKEN)
    with pytest.raises(actions.ActionError) as e:
        actions.invoke("fakenotes", "create_note", {"title": "hola"})
    assert e.value.code == "write_not_permitted"
    base.set_permission("fakenotes", "write")
    q = actions.invoke("fakenotes", "create_note", {"title": "hola"})
    assert q["status"] == "confirmation_required" and env["rec"].writes() == []
    assert "crear nota «hola»" in q["summary"]
    done = actions.confirm(q["action_id"], q["payload_hash"])
    assert done["status"] == "done" and env["rec"].writes()[0]["body"] == {"title": "hola"}
    assert env["rec"].writes()[0]["auth"] == f"Bearer {FAKE_TOKEN}"
    q2 = actions.invoke("fakenotes", "create_note", {"title": "otra"})
    base.set_permission("fakenotes", "read")                             # downgrade expires pending
    with pytest.raises(actions.ActionError):
        actions.confirm(q2["action_id"], q2["payload_hash"])
    assert len(env["rec"].writes()) == 1


def test_config_validation(env):
    make_fake("keychain")
    assert base.save_config("fakenotes", {"folders": "work\nhome\nwork"})["config"] == {"folders": ["work", "home"]}
    with pytest.raises(actions.ActionError) as e:
        base.save_config("fakenotes", {"folders": "../etc"})
    assert e.value.code == "bad_config"
    state = base._state_path("fakenotes")
    assert_mode(state, 0o600)
    assert FAKE_TOKEN not in state.read_text()


def test_none_mode_and_disconnect(env):
    make_fake("none")
    env["rec"].entries.insert(0, {"method": "GET", "url": "https://api.notes.fake.test/me", "match_auth": None,
                                  "json": {"login": "public"}})
    base.connect("fakenotes")
    assert env["rec"].calls[-1]["auth"] is None                          # no Authorization header at all
    assert store.get_account("fakenotes")["status"] == "connected"
    from ops_dashboard import connector_oauth as oauth
    oauth.disconnect("fakenotes")
    with pytest.raises(actions.ActionError) as e:
        base.run_sync("fakenotes")
    assert e.value.code == "not_connected"


# ── routes ──────────────────────────────────────────────────────────────────

@pytest.fixture
def server(env):
    from ops_dashboard.server import DashboardHandler
    make_fake("keychain")

    class Q(DashboardHandler):
        def log_message(self, *_a):
            pass
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", env
    finally:
        srv.shutdown()
        srv.server_close()


def _req(url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if data is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_routes_end_to_end(server):
    base_url, env = server
    st, b = _req(base_url + "/api/connectors")
    assert st == 200 and any(c["id"] == "fakenotes" and c["auth"] == "token" for c in b["connectors"])
    st, b = _req(base_url + "/api/connectors/token")
    assert b["connectors"]["fakenotes"]["token_mode"] == "keychain"
    st, b = _req(base_url + "/api/connectors/token/fakenotes/token", {"token": "wrong"})
    assert st == 401 and b["error"] == "unauthorized"
    st, b = _req(base_url + "/api/connectors/token/fakenotes/token", {"token": FAKE_TOKEN})
    assert st == 200 and b["connector"]["status"] == "connected" and FAKE_TOKEN not in json.dumps(b)
    st, b = _req(base_url + "/api/connectors/token/fakenotes/sync", {})
    assert st == 200 and b["sync"]["fetched"] == 2 and b["documents"] == 2
    st, b = _req(base_url + "/api/connectors/token/fakenotes/permission", {"permission": "write"})
    assert st == 200 and b["connector"]["permission"] == "write"
    st, b = _req(base_url + "/api/connectors/token/fakenotes/forget", {})
    assert st == 200 and b["deleted"] == 2
    st, b = _req(base_url + "/api/connectors/fakenotes/disconnect", {})
    assert st == 200 and b["connector"]["status"] == "disconnected"
    assert FAKE_TOKEN not in json.dumps(_req(base_url + "/api/connectors/audit")[1])


def test_routes_guard(server):
    base_url, env = server
    for path in ("/api/connectors/token/fakenotes/token", "/api/connectors/token/fakenotes/sync"):
        st, _b = _req(base_url + path, {"token": FAKE_TOKEN}, {"Origin": "https://evil.example"})
        assert st == 403
    assert _req(base_url + "/api/connectors/token", headers={"Host": "evil.example"})[0] == 421
    assert _req(base_url + "/api/connectors/token/nope/sync", {})[0] == 404
    assert env["kc"].items == {}


# ── page script ─────────────────────────────────────────────────────────────

def _js():
    src = (WEB / "connectors-token.js").read_text(encoding="utf-8")
    return re.sub(r"//[^\n]*", "", src)


def test_native_script_rules_and_i18n_parity():
    js = _js()
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                "setAttribute('on", 'setAttribute("on'):
        assert bad not in js, bad
    from ops_dashboard.server import DashboardHandler  # noqa: F401  (page + script are served)
    es_part, en_part = js.split("en: {")[0], js.split("en: {")[1].split("\n    }")[0]
    es = set(re.findall(r"\b(n_[a-z_]+):", es_part))
    en = set(re.findall(r"\b(n_[a-z_]+):", en_part))
    assert es and es == en
    used = set(re.findall(r"tr\(H, '(n_[a-z_]+)'", js))
    assert used <= es, used - es
    html = (WEB / "connectors.html").read_text(encoding="utf-8")
    assert '<script src="/connectors-token.js" defer></script>' in html
    assert "window.CLToken.render(c," in (WEB / "connectors.js").read_text(encoding="utf-8")


def test_raw_key_auth_scheme(tmp_path):
    from ops_dashboard import connector_registry as registry
    spec = registry.ConnectorSpec(id="fakeraw", name="Raw", auth="token", allowed_hosts=("api.raw.fake.test",))
    nc = base.TokenConnector(spec=spec, token_mode="keychain", whoami=lambda c: "", sync=lambda c, cfg: [],
                             auth_scheme="")
    rec = Recorded([{"method": "POST", "url": "https://api.raw.fake.test/graphql", "match_auth": "raw-key-1",
                     "match_partial": {"operationName": "Me"}, "json": {"data": {"ok": True}}}])
    with token_env(tmp_path, rec):
        assert base.TokenClient(nc, "raw-key-1").post_json("https://api.raw.fake.test/graphql",
                                                           {"operationName": "Me", "query": "q"}) == {"data": {"ok": True}}
        with pytest.raises(AssertionError):
            base.TokenClient(nc, "raw-key-1").post_json("https://api.raw.fake.test/graphql", {"operationName": "X"})


def test_snapshot_sync_removes_vanished_items_unless_partial(tmp_path, monkeypatch):
    from ops_dashboard import db
    from ops_dashboard.connectors_token import todoist
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "snap.db")
    db.init_db()
    nc = todoist.CONNECTOR
    a = base.Item(kind="task", key="A", title="a", content="a", updated=1)
    b = base.Item(kind="task", key="B", title="b", content="b", updated=1)

    def paths():
        with db.db() as conn:
            return sorted(r[0] for r in conn.execute("SELECT path FROM documents WHERE source='todoist'"))
    base.ingest(nc, [a, b])
    assert paths() == ["todoist/task/A", "todoist/task/B"]
    assert base.ingest(nc, [a, base.PARTIAL])["inserted_or_updated"] == 0   # partial: B kept
    assert paths() == ["todoist/task/A", "todoist/task/B"]
    assert base.ingest(nc, [a])["removed"] == 1                             # B completed upstream
    assert paths() == ["todoist/task/A"]


def test_same_size_same_time_edit_still_updates(tmp_path, monkeypatch):
    from ops_dashboard import db
    from ops_dashboard.connectors_token import readwise
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "edit.db")
    db.init_db()
    nc = readwise.CONNECTOR
    one = base.Item(kind="book", key="1", title="Libro", content="> hola", updated=5, tags=("a",))
    base.ingest(nc, [one])
    retag = base.Item(kind="book", key="1", title="Libro", content="> hola", updated=5, tags=("b",))
    assert base.ingest(nc, [retag])["inserted_or_updated"] == 1             # tag-only change
    assert base.ingest(nc, [retag])["inserted_or_updated"] == 0             # truly unchanged
    edit = base.Item(kind="book", key="1", title="Libro", content="> HOLA", updated=5, tags=("b",))
    assert base.ingest(nc, [edit])["inserted_or_updated"] == 1              # same length, same time


def test_rate_limit_403_is_not_unauthorized():
    import httpx

    from ops_dashboard.connectors_token import github
    c = base.TokenClient.__new__(base.TokenClient)
    c.nc = github.CONNECTOR
    limited = httpx.Response(403, headers={"x-ratelimit-remaining": "0"},
                             content=b'{"message": "API rate limit exceeded"}')
    with pytest.raises(base.TokenError) as e:
        c._check(limited)
    assert e.value.code == "rate_limited"
    with pytest.raises(base.TokenError) as e:
        c._check(httpx.Response(403, content=b'{"message": "Resource not accessible"}'))
    assert e.value.code == "unauthorized"
