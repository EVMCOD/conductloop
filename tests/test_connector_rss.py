"""RSS / Atom connector (no auth): user-configured https feeds on public IPs
only, same-host redirects only, DOCTYPE/ENTITY refused, HTML reduced to text,
entries ingested as untrusted, no writes. Recorded responses, no network."""
from __future__ import annotations

import json

import pytest
from token_fake import Recorded, token_env

pytest.importorskip("cryptography")

from ops_dashboard import connector_actions as actions  # noqa: E402
from ops_dashboard import connector_store as store  # noqa: E402
from ops_dashboard.connectors_token import base, rss  # noqa: E402

PUBLIC = {"blog.example.org": ["93.184.216.34"], "news.example.net": ["2606:2800:220:1:248:1893:25c8:1946"],
          "bomb.example.org": ["93.184.216.35"], "moved.example.org": ["93.184.216.36"],
          "intranet.example.org": ["10.1.2.3"], "meta.example.org": ["169.254.169.254"]}


@pytest.fixture
def feed(tmp_path):
    with token_env(tmp_path, Recorded.load("rss/recorded.json"), resolver=lambda h: PUBLIC[h]) as env:
        base.register_connector(rss.CONNECTOR)
        yield env


def _docs(db):
    with db.db() as conn:
        return {r[1]: (r[0], r[2], json.loads(r[3])) for r in conn.execute(
            "SELECT path, title, content, metadata FROM documents WHERE source = 'rss'")}


def _setup(feeds):
    base.save_config("rss", {"feeds": "\n".join(feeds)})
    base.connect("rss")


def test_sync_rss2_and_atom(feed):
    from ops_dashboard import db
    _setup(["https://blog.example.org/feed", "https://news.example.net/atom.xml"])
    assert store.get_account("rss")["account_label"] == "2 feed(s)"
    assert base.run_sync("rss") == {"fetched": 3, "inserted_or_updated": 3, "unchanged": 0}
    docs = _docs(db)
    path, content, meta = docs["Blog de Ops — Lanzamos conectores"]
    assert path.startswith("rss/entry/") and "Hoy lanzamos conectores con token." in content
    assert "alert(1)" not in content and "<" not in content and meta["untrusted"] is True
    assert meta["url"] == "https://blog.example.org/conectores" and "blog.example.org" in meta["tags"]
    assert "Búsqueda con acentos" in docs["Blog de Ops — Notas de la versión 2.1"][1]
    _p, content3, meta3 = docs["Changelog — Release 3.0"]
    assert meta3["url"] == "https://news.example.net/releases/3.0" and "Novedades: MCP y Linear." in content3
    assert all(c["auth"] is None for c in feed["rec"].calls)              # never an Authorization header
    assert [c["url"] for c in feed["rec"].calls][:2] == ["https://blog.example.org/feed", "https://blog.example.org/feed/"]
    assert base.run_sync("rss")["unchanged"] == 3


def test_entity_bomb_refused_before_parsing(feed, monkeypatch):
    _setup(["https://bomb.example.org/rss"])
    monkeypatch.setattr(rss.ET, "fromstring", lambda *_a: (_ for _ in ()).throw(AssertionError("parsed")))
    with pytest.raises(actions.ActionError) as e:
        base.run_sync("rss")
    assert e.value.code == "unsafe_xml"


def test_cross_host_redirect_refused(feed):
    _setup(["https://moved.example.org/rss"])
    with pytest.raises(actions.ActionError) as e:
        base.run_sync("rss")
    assert e.value.code == "redirect_refused" and all("evil" not in c["url"] for c in feed["rec"].calls)


@pytest.mark.parametrize("host", ["intranet.example.org", "meta.example.org"])
def test_private_hosts_refused_without_a_request(feed, host):
    _setup([f"https://{host}/rss"])
    with pytest.raises(actions.ActionError) as e:
        base.run_sync("rss")
    assert e.value.code == "host_not_public" and feed["rec"].calls == []


def test_one_bad_feed_does_not_block_the_rest(feed):
    _setup(["https://bomb.example.org/rss", "https://news.example.net/atom.xml"])
    assert base.run_sync("rss")["fetched"] == 1


def test_config_and_no_writes(feed):
    with pytest.raises(actions.ActionError):
        base.save_config("rss", {"feeds": "http://blog.example.org/feed"})
    with pytest.raises(actions.ActionError):
        base.save_config("rss", {"feeds": "file:///etc/passwd"})
    _setup(["https://blog.example.org/feed"])
    with pytest.raises(actions.ActionError) as e:
        base.set_permission("rss", "write")
    assert e.value.code == "no_write_actions"
    assert [a.kind for a in rss.CONNECTOR.spec.actions.values()] == ["read"]
    with pytest.raises(actions.ActionError):
        base.TokenClient(rss.CONNECTOR, "", rss.feed_hosts(base.config_of(rss.CONNECTOR))).get_text(
            "https://evil.example.com/rss")


def test_html_to_text():
    assert rss.html_to_text("<p>a<b>b</b></p><style>x{}</style><script>y()</script><p>c &amp; d</p>") == "ab\nc & d"


@pytest.mark.parametrize("enc", ["utf-16", "utf-16-be", "utf-32"])
def test_non_ascii_compatible_feeds_are_refused_before_parsing(enc):
    # In UTF-16/32 the DOCTYPE/ENTITY byte check cannot see the declaration,
    # while expat would still expand the entity.
    doc = (f'<?xml version="1.0" encoding="{enc}"?><!DOCTYPE r [<!ENTITY a "boom">]>'
           '<rss><channel><title>&a;</title></channel></rss>').encode(enc)
    with pytest.raises(base.TokenError) as e:
        rss.parse_feed(doc, "https://feeds.example/x")
    assert e.value.code == "unsafe_xml"
    declared = b'<?xml version="1.0" encoding="cp037"?><rss/>'
    with pytest.raises(base.TokenError):
        rss.parse_feed(declared, "https://feeds.example/x")


def test_atom_xhtml_keeps_paragraphs_and_drops_script():
    doc = (b'<feed xmlns="http://www.w3.org/2005/Atom"><title>T</title><entry><title>E</title><id>1</id>'
           b'<content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>A</p><p>B</p>'
           b'<script>steal()</script></div></content></entry></feed>')
    _, entries = rss.parse_feed(doc, "https://feeds.example/x")
    assert entries[0]["body"] == "A\nB"
