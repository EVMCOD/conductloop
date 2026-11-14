"""Google Drive connector (read + ingest) and the shared Google BYOK app. No
network: recorded Drive API responses behind ProviderFake; loopback only."""
from __future__ import annotations

import json
import socket

import httpx
import pytest
from connectors_native_fake import NetworkForbidden, ProviderFake, connect, recorded, sqlite_text

from ops_dashboard import connector_actions as actions
from ops_dashboard import connector_oauth as oauth
from ops_dashboard import connector_registry as registry
from ops_dashboard import connector_store as store
from ops_dashboard import connector_tokens as tokens
from ops_dashboard import connectors_native
from ops_dashboard.connectors_native import google_drive as drive

REC = recorded("google_drive")
API = "www.googleapis.com/drive/v3"
SECRET = "GOCSPX-fake-desktop-secret"


@pytest.fixture(autouse=True)
def _no_network():
    real = socket.socket.connect

    def guarded(self, address):
        if (address[0] if isinstance(address, tuple) else address) not in ("127.0.0.1", "::1", "localhost"):
            raise NetworkForbidden(f"network access in tests: {address!r}")
        return real(self, address)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", guarded)
        yield


def _list(request: httpx.Request) -> httpx.Response:
    q = request.url.params
    assert "trashed = false" in q["q"] and q["fields"].startswith("nextPageToken,files(")
    return httpx.Response(200, json=REC["list_page2" if q.get("pageToken") == "p2" else "list_page1"])


def _sheet_html(rows: list[list[str]]) -> str:
    """A worksheet the way Sheets' HTML export lays it out: an A, B, C… header
    row and a row-number column around the cells."""
    cols = max(len(r) for r in rows)
    head = "<tr><th></th>" + "".join(f"<th>{chr(65 + i)}</th>" for i in range(cols)) + "</tr>"
    body = "".join(f"<tr><th>{n}</th>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
                   for n, r in enumerate(rows, 1))
    return f"<html><body><table>{head}{body}</table></body></html>"


def _sheets_zip(csv_text: str) -> bytes:
    import csv
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Ventas.html", _sheet_html(list(csv.reader(io.StringIO(csv_text)))))
        z.writestr("Costes.html", _sheet_html([["concepto", "importe"], ["hosting", "40"]]))
        z.writestr("resources/sheet.css", "td{}")
    return buf.getvalue()


def _file(request: httpx.Request) -> httpx.Response:
    parts = request.url.path.split("/")
    if parts[-1] == "export":
        fid = parts[-2]
        if fid == "1SheetBB":            # CSV would carry only the first worksheet
            assert request.url.params["mimeType"] == "application/zip"
            return httpx.Response(200, content=_sheets_zip(REC[f"export_{fid}"]))
        assert request.url.params["mimeType"] == "text/plain"
        return httpx.Response(200, text=REC[f"export_{fid}"])
    fid = parts[-1]
    if request.url.params.get("alt") == "media":
        assert fid == "1NotesCC", "only small text files are downloaded"
        return httpx.Response(200, text=REC[f"media_{fid}"])
    meta = next(f for k in ("list_page1", "list_page2") for f in REC[k]["files"] if f["id"] == fid)
    return httpx.Response(200, json=meta)


def _run(cid, action, payload):
    return actions.invoke(cid, action, payload)["result"]


def make_fake() -> ProviderFake:
    routes: dict = {("GET", f"{API}/files"): _list}
    for fid in ("1DocAAA", "1SheetBB", "1NotesCC", "1PdfDD", "1BigEE"):
        routes[("GET", f"{API}/files/{fid}")] = _file
        routes[("GET", f"{API}/files/{fid}/export")] = _file
    return ProviderFake(token_url="https://oauth2.googleapis.com/token",
                        hosts=("oauth2.googleapis.com", "accounts.google.com", "www.googleapis.com"),
                        client_secret=SECRET, routes=routes)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "drive.db")
    db.init_db()
    fake = make_fake()
    prev_t = oauth.set_transport(fake.transport())
    prev_b = tokens.use_backend(tokens.MemoryBackend())
    try:
        yield fake, db
    finally:
        tokens.use_backend(prev_b)
        oauth.set_transport(prev_t)


def test_spec_read_only_minimal_scope():
    spec = registry.get("google_drive")
    assert spec is drive.SPEC and spec.client_secret_required and spec.revoke_url == ""
    assert spec.read_scopes == ("https://www.googleapis.com/auth/drive.readonly",)
    assert spec.write_scopes == () and all(a.kind == "read" for a in spec.actions.values())
    assert set(spec.allowed_hosts) == {"accounts.google.com", "oauth2.googleapis.com", "www.googleapis.com"}


def test_connect_asks_only_drive_readonly(env):
    fake, _ = env
    params = connect("google_drive", fake, client_secret=SECRET)
    assert params["scope"] == "https://www.googleapis.com/auth/drive.readonly"
    assert params["redirect_uri"].startswith("http://127.0.0.1:") and params["code_challenge_method"] == "S256"


def test_sync_ingests_readable_files_idempotently(env):
    fake, db = env
    connect("google_drive", fake, client_secret=SECRET)
    first = _run("google_drive", "sync", {"days": 365})
    assert first == {"files": 5, "vault_updated": 3, "skipped": 2}
    again = _run("google_drive", "sync", {"days": 365})
    assert again == {"files": 5, "vault_updated": 0, "skipped": 2}
    with db.db() as c:
        rows = {r["path"]: r for r in c.execute("SELECT * FROM documents WHERE source='google_drive'")}
    assert set(rows) == {"google_drive/1DocAAA", "google_drive/1SheetBB", "google_drive/1NotesCC"}
    assert "software,1200" in rows["google_drive/1SheetBB"]["content"]
    assert "## Costes" in rows["google_drive/1SheetBB"]["content"]          # every worksheet
    assert "hosting,40" in rows["google_drive/1SheetBB"]["content"]
    assert "revisar #51" in rows["google_drive/1NotesCC"]["content"]
    meta = json.loads(rows["google_drive/1DocAAA"]["metadata"])
    assert meta["link"].startswith("https://docs.google.com/") and meta["kind"] == "file"
    paths = [r.url.path for r in fake.requests]
    assert not any("1PdfDD" in p or "1BigEE" in p for p in paths), "PDFs and big files are never downloaded"
    assert all(r.method in ("GET", "POST") for r in fake.requests)
    assert [r for r in fake.requests if r.method == "POST"] == [fake.requests[0]]  # only the token exchange


def test_drive_text_cannot_trigger_writes(env):
    fake, _ = env
    connect("google_drive", fake, client_secret=SECRET)
    _run("google_drive", "sync", {"days": 365})
    assert actions.list_actions("pending") == []
    assert "send" not in " ".join(drive.SPEC.actions)


def test_list_and_get_file(env):
    fake, _ = env
    connect("google_drive", fake, client_secret=SECRET)
    files = _run("google_drive", "list_files", {"name": "Plan' or '1'='1", "max": 3})["files"]
    q = [r for r in fake.requests if r.url.path.endswith("/files")][0].url.params["q"]
    assert "name contains 'Plan\\' or \\'1\\'=\\'1'" in q
    assert len(files) == 3 and "text" not in files[0]
    got = _run("google_drive", "get_file", {"id": "1PdfDD"})
    assert got["readable"] is False and got["text"] is None
    got = _run("google_drive", "get_file", {"id": "1DocAAA"})
    assert got["text"].startswith("Plan Q4")
    with pytest.raises(actions.ActionError):
        _run("google_drive", "get_file", {"id": "../../users/me"})


def test_tokens_and_secret_never_in_sqlite(env, tmp_path):
    fake, db = env
    connect("google_drive", fake, client_secret=SECRET)
    _run("google_drive", "sync", {"days": 365})
    dump = sqlite_text(db.DB_PATH)
    assert SECRET not in dump and "fake-rt" not in dump and "fake-at-" not in dump


# ---------- one BYOK app for the three Google connectors ----------

def test_google_group_has_the_three_connectors():
    assert sorted(connectors_native.CLIENT_GROUPS["google"]) == ["gmail", "google_calendar", "google_drive"]


def test_share_client_copies_credentials_but_never_tokens(env):
    fake, _ = env
    connect("google_drive", fake, client_id="one-google-app", client_secret=SECRET)
    drive_token = tokens.load_token("google_drive")
    shared = connectors_native.share_client("google", "google_drive")
    assert sorted(shared) == ["gmail", "google_calendar"]
    for cid in shared:
        assert store.get_account(cid)["client_id"] == "one-google-app"
        assert tokens.load_client_secret(cid) == SECRET
        assert tokens.load_token(cid) is None, "sharing the app must not share consent/tokens"
        assert store.get_account(cid)["status"] != "connected"
    assert tokens.load_token("google_drive") == drive_token
    audit = store.list_audit(connector_id="gmail")
    assert audit[0]["event"] == "client_saved" and SECRET not in json.dumps(audit)
    # each connector still asks for its own scopes with the shared client
    flow = oauth.start("gmail", "read", listen=False)
    assert "client_id=one-google-app" in flow["authorize_url"]
    assert "gmail.readonly" in flow["authorize_url"] and "drive" not in flow["authorize_url"]


def test_share_client_never_overwrites_another_configured_app(env):
    store.upsert_account("gmail", client_id="older-app")         # gmail set up with another app
    tokens.save_client_secret("gmail", "older-secret")
    store.upsert_account("google_drive", client_id="one-google-app")
    tokens.save_client_secret("google_drive", SECRET)
    shared = connectors_native.share_client("google", "google_drive")
    assert shared == ["google_calendar"]                       # gmail keeps the client its token needs
    assert store.get_account("gmail")["client_id"] == "older-app"
    assert tokens.load_client_secret("gmail") == "older-secret"


def test_google_asks_for_consent_so_a_reconnect_gets_a_refresh_token(env):
    store.upsert_account("gmail", client_id="cid")
    tokens.save_client_secret("gmail", SECRET)
    url = oauth.start("gmail", "read", listen=False)["authorize_url"]
    assert "prompt=consent" in url and "access_type=offline" in url


def test_share_client_errors(env):
    with pytest.raises(actions.ActionError) as e:
        connectors_native.share_client("google", "google_drive")
    assert e.value.code == "client_id_required"
    store.upsert_account("google_drive", client_id="x")
    with pytest.raises(actions.ActionError) as e:
        connectors_native.share_client("google", "google_drive")
    assert e.value.code == "client_secret_required"
    with pytest.raises(actions.ActionError) as e:
        connectors_native.share_client("google", "notion")
    assert e.value.code == "not_in_group"


def test_share_route_is_local_only(env):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from ops_dashboard.server import DashboardHandler
    store.upsert_account("google_calendar", client_id="one-google-app")
    tokens.save_client_secret("google_calendar", SECRET)

    class _Q(DashboardHandler):
        def log_message(self, *_a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        code, listing = call("/api/connectors-sync")
        assert code == 200 and "google_drive" in listing["client_groups"]["google"]
        assert call("/api/connectors-client-group/google", {"from": "google_calendar"},
                    {"Origin": "https://evil.example"})[0] == 403
        assert store.get_account("gmail")["client_id"] in (None, "")
        code, body = call("/api/connectors-client-group/google", {"from": "google_calendar"})
        assert code == 200 and sorted(body["shared_with"]) == ["gmail", "google_drive"]
        assert SECRET not in json.dumps(body)
        assert call("/api/connectors-client-group/google", {"from": "notion"})[0] == 400
    finally:
        srv.shutdown()
        srv.server_close()
