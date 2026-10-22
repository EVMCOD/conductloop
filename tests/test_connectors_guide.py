"""BYOK guide on /connectors (connectors-guide.js): static safety rules, ES/EN
parity, the redirect contract the native connectors implement, the warnings
that must be there, and a DOM render in Node (skipped without node).
No network."""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "ops_dashboard" / "web"
JS = (WEB / "connectors-guide.js").read_text(encoding="utf-8")
CODE = re.sub(r"(?m)^\s*//.*$", "", JS)

# Redirect contract shared with the native connectors (their tests import these).
REDIRECTS = {
    "google": "http://127.0.0.1:<",  # free port, Desktop client, nothing registered
    "microsoft": "http://localhost/callback",
    "slack": "http://localhost:' + SLACK_PORT + '/callback",
    "notion": "Not applicable: Notion connects with a token",
}
SLACK_PORT = 47823
NOTION_PORT = 47824
ALLOWED_LINK_HOSTS = {
    "console.cloud.google.com",
    "developers.google.com",
    "support.google.com",
    "entra.microsoft.com",
    "learn.microsoft.com",
    "api.slack.com",
    "docs.slack.dev",
    "app.notion.com",
    "developers.notion.com",
}


def _provider_block(pid: str) -> str:
    start = CODE.index(f"id: '{pid}'")
    nxt = [CODE.index(f"id: '{o}'") for o in REDIRECTS if o != pid and CODE.index(f"id: '{o}'") > start]
    return CODE[start : min(nxt) if nxt else CODE.index("var T = ES ?")]


def test_static_rules_textcontent_only():
    for banned in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "setTimeout('",
        'setTimeout("',
        "fetch(",
    ):
        assert banned not in CODE, banned
    assert "rel = 'noopener noreferrer'" in CODE and "^https:" in CODE


def test_page_loads_guide_without_inline_script():
    html = (WEB / "connectors.html").read_text(encoding="utf-8")
    assert '<script src="/connectors-guide.js" defer></script>' in html
    assert 'id="guide-card"' in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert " style=" not in html and not re.search(r"\son[a-z]+=", html)


def test_all_four_providers_with_steps_scopes_warnings_in_es_and_en():
    for pid in REDIRECTS:
        b = _provider_block(pid)
        for part in ("steps:", "scopes:", "warnings:", "sources:", "redirect:", "console:"):
            assert part in b, (pid, part)
        assert b.count("es: '") == b.count("en: '") >= 8, pid


def test_redirect_contract():
    assert f"var SLACK_PORT = {SLACK_PORT};" in CODE and f"var NOTION_PORT = {NOTION_PORT};" in CODE
    for pid, redirect in REDIRECTS.items():
        assert redirect in _provider_block(pid), pid
    assert "/callback" in _provider_block("google")


def test_required_warnings_present():
    g, s, n, m = (_provider_block(p) for p in ("google", "slack", "notion", "microsoft"))
    # Google: restricted Gmail scopes + 100-user cap of unverified apps (+ 7-day Testing expiry)
    assert "gmail.readonly" in g and "RESTRINGIDO" in g and "RESTRICTED" in g
    assert "100 usuarios nuevos" in g and "100 new users" in g and "7 días" in g
    # Slack: fixed port + user scopes only
    assert "Puerto fijo" in s and "Fixed port" in s
    assert "Solo permisos de usuario" in s and "User permissions only" in s and "pkce_enabled: true" in s
    assert "Bot Token Scope" in s
    # Notion: token only (the connector has no OAuth flow); OAuth is not offered
    assert "token" in n and "todavía no está disponible" in n and "not available in ConductLoop yet" in n
    # Microsoft: no secret, localhost because 127.0.0.1/http needs the manifest
    assert "replyUrlsWithType" in m and "Certificates & secrets" in m


def test_minimal_scopes_split_read_write():
    expected = {
        "google": (
            {
                "https://www.googleapis.com/auth/calendar.events.readonly",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/drive.readonly",
            },
            {"https://www.googleapis.com/auth/gmail.send"},
        ),
        "microsoft": (
            {"offline_access", "User.Read", "Mail.Read", "Calendars.Read"},
            {"Mail.Send", "Calendars.ReadWrite"},
        ),
        "slack": ({"channels:read", "channels:history", "users:read"}, {"chat:write"}),
    }
    for pid, (read, write) in expected.items():
        b = _provider_block(pid)
        got = dict(re.findall(r"scope: '([^']+)', kind: '(read|write)'", b))
        assert read <= {k for k, v in got.items() if v == "read"}, pid
        assert write <= {k for k, v in got.items() if v == "write"}, pid
        assert not {"https://mail.google.com/", "https://www.googleapis.com/auth/drive", "Mail.ReadWrite"} & set(got)


def test_unverified_marker_only_from_flag():
    # ⚠️ is only ever added by mark() for `unverified: true` items (and the legend).
    assert CODE.count("⚠️") == 3  # legend es, legend en, mark()
    assert CODE.count("unverified: true") >= 8


def test_links_are_https_on_provider_domains():
    # Links only come from `console` and `sources` (scopes and redirects are shown as text).
    urls = re.findall(r"console: '([a-z]+://[^']+)'", CODE)
    for block in re.findall(r"sources: \[(.*?)\]", CODE, re.S):
        urls += re.findall(r"'([^']+)'", block)
    assert len(urls) > 20
    for u in urls:
        p = urlparse(u)
        assert p.scheme == "https" and p.hostname in ALLOWED_LINK_HOSTS, u


def test_doc_lists_same_contract():
    doc = (ROOT / "docs" / "connectors" / "oauth-byok-guide.md").read_text(encoding="utf-8")
    for s in (
        "http://localhost/callback",
        f"http://localhost:{SLACK_PORT}/callback",
        "Notion — solo token (OAuth no implementado)",
        "http://127.0.0.1:<puerto libre>/callback",
        "⚠️",
        "Probar en el Mac",
    ):
        assert s in doc, s


def test_served_by_local_server(tmp_path, monkeypatch):
    from ops_dashboard import db
    from ops_dashboard.server import DashboardHandler

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "g.db")
    db.init_db()

    class _Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/connectors-guide.js", timeout=8) as r:
            assert r.status == 200 and b"ConductLoopGuide" in r.read()
    finally:
        srv.shutdown()
        srv.server_close()


_DOM_SHIM = r"""
const vm = require('vm'); const fs = require('fs');
function Node(tag){ this.tagName=tag; this.children=[]; this.attrs={}; this.className=''; this._text=''; }
Node.prototype.appendChild=function(c){ this.children.push(c); return c; };
Node.prototype.removeChild=function(c){ this.children.splice(this.children.indexOf(c),1); };
Object.defineProperty(Node.prototype,'firstChild',{get(){return this.children[0]||null;}});
Object.defineProperty(Node.prototype,'textContent',{get(){return this._text+this.children.map(c=>c.textContent).join('');},set(v){this._text=String(v);this.children=[];}});
Node.prototype.setAttribute=function(k,v){ this.attrs[k]=String(v); };
for (const bad of ['innerHTML','outerHTML']) Object.defineProperty(Node.prototype,bad,{set(){throw new Error(bad+' used');}});
const card = new Node('div'); let onReady = null;
const document = { createElement:t=>new Node(t), createTextNode:t=>{const n=new Node('#text'); n._text=String(t); return n;},
  getElementById:id=>id==='guide-card'?card:null, addEventListener:(e,f)=>{onReady=f;} };
const ctx = { document, navigator:{language:process.argv[3]}, window:{} };
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'), ctx); onReady();
function walk(n,out){ out.push(n); n.children.forEach(c=>walk(c,out)); return out; }
const all = walk(card,[]);
const links = all.filter(n=>n.tagName==='a').map(n=>({href:n.href, rel:n.rel, target:n.target}));
const details = all.filter(n=>n.tagName==='details').map(n=>n.attrs['data-guide']);
console.log(JSON.stringify({text:card.textContent, links, details, api:ctx.window.ConductLoopGuide}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("lang", ["es-ES", "en-US"])
def test_renders_in_dom(tmp_path, lang):
    shim = tmp_path / "shim.js"
    shim.write_text(_DOM_SHIM, encoding="utf-8")
    out = subprocess.run(
        ["node", str(shim), str(WEB / "connectors-guide.js"), lang],  # noqa: S603,S607
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    res = json.loads(out)
    assert res["details"] == ["google", "microsoft", "slack", "notion"]
    assert res["api"]["slackPort"] == SLACK_PORT and res["api"]["notionPort"] == NOTION_PORT
    assert all(link["href"].startswith("https://") and link["rel"] == "noopener noreferrer" for link in res["links"])
    text = res["text"]
    assert "⚠️" in text
    if lang.startswith("es"):
        assert "Guía: crea tu propia app OAuth" in text and "Puerto fijo" in text and "Fixed port" not in text
    else:
        assert "Guide: create your own OAuth app" in text and "Fixed port" in text and "Puerto fijo" not in text
