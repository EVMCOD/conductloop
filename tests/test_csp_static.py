"""CSP checks that run in CI without a Mac, a WebView or a browser.

Two policies exist, and they govern different things (docs/CSP.md):

* `app.security.csp` in desktop/src-tauri/tauri.conf.json: Tauri adds it to the
  assets it serves itself, i.e. the loading page (desktop/src-tauri/loading).
* `LEGACY_PAGE_CSP` / the strict `PAGE_CSP` of the local server: the dashboard
  UI is loaded from http://localhost:7777, so these headers are what the
  WebView enforces on it.

What is checked here: the JSON parses and the policies are not null/wildcard,
carry the minimum directives and never allow eval; no UI file loads anything
from outside the local server; and every inline script, event handler and style
is covered by the policy of the page that contains it (a page on a strict
policy may have none; the legacy pages may not get more than today's count).
"""
from __future__ import annotations

import json
import re
import socket
import threading
import urllib.request
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "ops_dashboard" / "web"
TAURI = ROOT / "desktop" / "src-tauri"
LOADING = TAURI / "loading"
CONF_PATH = TAURI / "tauri.conf.json"

# Pages served with the strict PAGE_CSP (default-src 'self', script-src 'self',
# style-src 'self'): zero inline anything. Every other web/*.html is "legacy".
STRICT_PAGES = {"sessions.html", "connectors.html", "learning.html", "chat.html"}

# Inline code that the legacy policy has to allow ('unsafe-inline') because the
# pages still contain it. A RATCHET: lower a number when you refactor, never
# raise it, and a file that is not listed here must have none. Counted by
# inline_markers() below (HTML via a parser; JS by looking for handler/style
# attributes inside string literals).
LEGACY_INLINE = {
    # file: (inline <script> blocks, on*= handlers, style= attributes, <style> blocks)
    "agents.html": (1, 0, 7, 1),
    "ai.html": (1, 0, 11, 1),
    "app.js": (0, 20, 970, 0),
    "boardroom-autonomy.html": (1, 0, 3, 1),
    "boardroom-create.html": (1, 1, 6, 1),
    "boardroom-directory.html": (1, 0, 1, 1),
    "boardroom-portfolio.html": (1, 0, 0, 1),
    "boardroom-recap.html": (1, 0, 8, 1),
    "boardroom-thread.html": (1, 0, 7, 1),
    "boardroom-vault.html": (1, 0, 0, 1),
    "boardroom.html": (1, 0, 0, 1),
    "brain.html": (1, 0, 2, 1),
    "browser.html": (1, 0, 1, 1),
    "credentials.html": (1, 0, 1, 1),
    "dialog.js": (0, 0, 13, 0),
    "index.html": (0, 9, 32, 0),
    "media.html": (1, 0, 0, 1),
    "note.html": (1, 0, 5, 1),
    "pair.html": (1, 0, 9, 1),
    "runs.html": (1, 1, 9, 1),
    "servers.html": (1, 0, 3, 1),
    "setup.html": (1, 0, 2, 1),
    "status.html": (1, 0, 0, 1),
    "tour.html": (1, 0, 0, 1),
}

# Known and NOT covered by the CSP: the voice-notes list builds
# <audio src="file://<audio_path>"> (app.js). A page served over http:// cannot
# load file: URLs in any browser, so that player never worked from the dashboard;
# the policy does not open file: for it. Fix = serve the file through a local
# route (media-src 'self'), then delete this entry.
KNOWN_FILE_AUDIO = {("app.js", '<audio controls src="file://')}

REQUIRED_TAURI = ("default-src", "script-src", "style-src", "img-src", "connect-src",
                  "object-src", "base-uri", "form-action", "frame-ancestors")
REQUIRED_SERVER = REQUIRED_TAURI + ("font-src", "media-src", "frame-src")
IPC_SOURCES = ("ipc:", "http://ipc.localhost")
# The only non-'self' sources any of our policies may contain.
ALLOWED_SOURCES = {"'self'", "'none'", "'unsafe-inline'", "data:", "blob:", *IPC_SOURCES}


# ── helpers ────────────────────────────────────────────────────────────────

def parse_csp(csp) -> dict[str, list[str]]:
    """A CSP given as a header string or as the object form of tauri.conf.json."""
    if isinstance(csp, dict):
        return {k.lower(): (v.split() if isinstance(v, str) else list(v)) for k, v in csp.items()}
    out: dict[str, list[str]] = {}
    for part in str(csp).split(";"):
        toks = part.split()
        if toks:
            out.setdefault(toks[0].lower(), toks[1:])
    return out


def tauri_csp():
    conf = json.loads(CONF_PATH.read_text(encoding="utf-8"))
    return conf, conf["app"]["security"]["csp"]


class _Scan(HTMLParser):
    """Collects what a policy would have to allow or forbid in one HTML file."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inline_scripts = 0
        self.style_blocks = 0
        self.handlers: list[str] = []
        self.style_attrs = 0
        self.javascript_urls = 0
        self.loads: list[tuple[str, str, str]] = []   # (tag, attr, value) that fetch a resource
        self.bad_tags: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script":
            if "src" in a:
                self.loads.append((tag, "src", a["src"]))
            elif a.get("type", "").lower() not in ("application/json", "application/ld+json", "importmap"):
                self.inline_scripts += 1
        elif tag == "style":
            self.style_blocks += 1
        elif tag in ("iframe", "frame", "object", "embed", "applet", "base"):
            self.bad_tags.append(tag)
        for k, v in a.items():
            if k.startswith("on"):
                self.handlers.append(k)
            elif k == "style":
                self.style_attrs += 1
            if v.strip().lower().startswith("javascript:"):
                self.javascript_urls += 1
        if tag == "link" and "href" in a:
            self.loads.append((tag, "href", a["href"]))
        if tag in ("img", "audio", "video", "source", "track", "input") and "src" in a:
            self.loads.append((tag, "src", a["src"]))
        if tag == "video" and "poster" in a:
            self.loads.append((tag, "poster", a["poster"]))
        if tag == "form" and "action" in a:
            self.loads.append((tag, "action", a["action"]))
        if tag in ("button", "input") and "formaction" in a:
            self.loads.append((tag, "formaction", a["formaction"]))
        if tag == "meta" and a.get("http-equiv", "").lower() in ("refresh", "content-security-policy"):
            self.bad_tags.append("meta http-equiv=" + a["http-equiv"])


def scan_html(path: Path) -> _Scan:
    s = _Scan()
    s.feed(path.read_text(encoding="utf-8"))
    return s


def is_external(url: str) -> bool:
    u = url.strip().lower()
    return bool(re.match(r"^([a-z][a-z0-9+.-]*:|//)", u)) and not u.startswith(("data:", "blob:"))


_JS_HANDLER = re.compile(r"""\bon[a-z]{3,}\s*=\s*(?:\\?["']|\\?&quot;)""", re.I)
_JS_STYLE_ATTR = re.compile(r"""\sstyle\s*=\s*\\?["']""")
_JS_SET_ATTR = re.compile(r"""setAttribute\(\s*['"](?:style|on[a-z]+)['"]""", re.I)
_JS_EVAL = re.compile(r"\beval\s*\(|\bnew\s+Function\b|\b(?:setTimeout|setInterval)\s*\(\s*['\"`]|document\.write")
_JS_NET_ABS = re.compile(
    r"""(?:fetch|EventSource|WebSocket|sendBeacon|importScripts|XMLHttpRequest[^;]*?\.open)\s*\(\s*(?:['"`](?:GET|POST|PUT|DELETE)['"`]\s*,\s*)?['"`]\s*(?:[a-z][a-z0-9+.-]*:)?//""",
    re.I)
_JS_SRC_ABS = re.compile(r"""\.(?:src|srcset|poster)\s*=\s*['"`]\s*(?:https?:|wss?:|ftp:|file:)?//""", re.I)
_JS_TAG_ABS = re.compile(
    r"""<(?:script|img|iframe|frame|link|audio|video|source|embed|object|form)\b[^>]*?\b(?:src|href|action|poster)\s*=\s*\\?["']?\s*(?:https?:|wss?:|file:)?//""",
    re.I)
_CSS_ABS = re.compile(r"""(?:url\(\s*['"]?|@import\s+['"]?)\s*(?:https?:|wss?:)?//""", re.I)


def strip_js_comments(src: str) -> str:
    return re.sub(r"^[ \t]*//.*$", "", src, flags=re.M)


def inline_markers(path: Path) -> tuple[int, int, int, int]:
    """(inline scripts, event handlers, style attributes, <style> blocks)."""
    if path.suffix == ".html":
        s = scan_html(path)
        return s.inline_scripts, len(s.handlers), s.style_attrs, s.style_blocks
    code = strip_js_comments(path.read_text(encoding="utf-8"))
    return 0, len(_JS_HANDLER.findall(code)) + len(_JS_SET_ATTR.findall(code)), len(_JS_STYLE_ATTR.findall(code)), 0


def ui_files() -> list[Path]:
    return sorted(p for p in WEB.iterdir() if p.suffix in (".html", ".js", ".css"))


def local_scripts(page: Path) -> list[Path]:
    out = []
    for tag, _attr, val in scan_html(page).loads:
        if tag == "script":
            f = WEB / val.split("?")[0].lstrip("/")
            if f.exists():
                out.append(f)
    return out


def check_policy(name: str, pol: dict[str, list[str]], required) -> None:
    for d in required:
        assert d in pol, f"{name}: missing directive {d}"
    for d, toks in pol.items():
        for t in toks:
            assert t != "*" and "*" not in t, f"{name}: wildcard in {d}"
            assert t not in ("http:", "https:", "ws:", "wss:", "file:", "filesystem:"), f"{name}: scheme-wide source {t} in {d}"
            assert t != "'unsafe-eval'" and t != "'wasm-unsafe-eval'", f"{name}: eval allowed in {d}"
            assert t != "'unsafe-hashes'", f"{name}: unsafe-hashes in {d}"
            assert t in ALLOWED_SOURCES, f"{name}: unexpected source {t} in {d}: allow-list it here with a reason"
    assert pol["object-src"] == ["'none'"] and pol["base-uri"] == ["'none'"]
    assert pol["frame-ancestors"] == ["'none'"]
    assert "'unsafe-inline'" not in pol.get("default-src", []), f"{name}: unsafe-inline in default-src"
    assert not any(t.startswith("http") and "localhost" not in t for toks in pol.values() for t in toks)


# ── tauri.conf.json ────────────────────────────────────────────────────────

def test_tauri_conf_parses_and_csp_is_set():
    conf, csp = tauri_csp()
    assert csp is not None, "csp must not be null"
    assert isinstance(csp, (str, dict)) and csp
    assert conf["app"]["security"].get("dangerousDisableAssetCspModification") in (None, False), \
        "Tauri's nonce/hash injection must stay on"


def test_tauri_csp_has_the_minimum_directives_and_no_wildcards():
    pol = parse_csp(tauri_csp()[1])
    check_policy("tauri.conf.json", pol, REQUIRED_TAURI)
    assert pol["default-src"] == ["'none'"]
    assert "'unsafe-inline'" not in pol["script-src"] and "'unsafe-inline'" not in pol["style-src"]
    assert all(s in pol["connect-src"] for s in IPC_SOURCES), "Tauri IPC (ipc: and http://ipc.localhost)"
    assert pol["form-action"] == ["'none'"] and pol["frame-src"] == ["'none'"]


def test_loading_page_needs_nothing_the_tauri_csp_forbids():
    """The only page Tauri serves itself. Tauri hashes/nonces <style> and local
    scripts, but not style="" attributes, handlers or javascript: URLs."""
    for page in LOADING.glob("*.html"):
        s = scan_html(page)
        assert s.inline_scripts == 0 and not s.handlers and s.style_attrs == 0 and s.javascript_urls == 0, page
        assert not s.bad_tags, (page, s.bad_tags)
        for tag, attr, val in s.loads:
            assert not is_external(val), (page, tag, attr, val)
    for js in LOADING.glob("*.js"):
        assert not _JS_EVAL.search(strip_js_comments(js.read_text(encoding="utf-8"))), js


def test_tauri_updater_and_backend_origin_are_not_a_webview_concern():
    """The updater runs in Rust (no WebView request, so no connect-src entry) and
    the window navigates to the local server (navigation is not governed by
    connect-src). If either changes, the policy must be revisited."""
    conf, _ = tauri_csp()
    assert all(u.startswith("https://") for u in conf["plugins"]["updater"]["endpoints"])
    lib = (TAURI / "src" / "lib.rs").read_text(encoding="utf-8")
    assert 'const BACKEND_URL: &str = "http://localhost:7777";' in lib
    cap = json.loads((TAURI / "capabilities" / "browser-ui.json").read_text(encoding="utf-8"))
    assert cap["remote"]["urls"] == ["http://localhost:7777/*"]


# ── the server's policies (what the WebView enforces on the real UI) ───────

def _server_policies():
    from ops_dashboard import routes_connectors, routes_learning, routes_sessions
    from ops_dashboard.server import LEGACY_PAGE_CSP
    return LEGACY_PAGE_CSP, {routes_learning.PAGE_CSP, routes_sessions.PAGE_CSP, routes_connectors.PAGE_CSP}


def test_legacy_server_csp_minimum_and_no_wildcards():
    legacy, _ = _server_policies()
    pol = parse_csp(legacy)
    check_policy("LEGACY_PAGE_CSP", pol, REQUIRED_SERVER)
    assert pol["default-src"] == ["'self'"]
    assert all(s in pol["connect-src"] for s in IPC_SOURCES) and "'self'" in pol["connect-src"]
    assert pol["frame-src"] == ["'none'"] and pol["form-action"] == ["'self'"]


def test_strict_server_csp_has_no_unsafe_inline_and_no_wildcards():
    _, stricts = _server_policies()
    assert len(stricts) == 1, "sessions, connectors, learning and chat share one strict policy"
    pol = parse_csp(next(iter(stricts)))
    check_policy("PAGE_CSP", pol, ("default-src", "script-src", "style-src", "img-src", "connect-src",
                                   "object-src", "base-uri", "form-action", "frame-ancestors"))
    assert "'unsafe-inline'" not in pol["script-src"] + pol["style-src"]


def test_tauri_ipc_users_are_covered_by_the_legacy_connect_src():
    legacy, _ = _server_policies()
    users = [p.name for p in WEB.iterdir() if p.suffix in (".html", ".js")
             and re.search(r"__TAURI(_INTERNALS)?__", p.read_text(encoding="utf-8"))]
    assert users, "no page talks to Tauri any more: the ipc sources can be dropped"
    for u in users:
        assert u not in STRICT_PAGES and u != "csrf.js"
    assert all(s in parse_csp(legacy)["connect-src"] for s in IPC_SOURCES)


# ── static scan of the UI: external origins and uncovered inline code ──────

def test_every_page_is_classified():
    pages = {p.name for p in WEB.glob("*.html")}
    listed = STRICT_PAGES | {f for f in LEGACY_INLINE if f.endswith(".html")}
    # Pages with no inline code at all need no entry in LEGACY_INLINE.
    unlisted = pages - listed
    for name in unlisted:
        assert inline_markers(WEB / name) == (0, 0, 0, 0), f"{name} has inline code: add it to LEGACY_INLINE"
    assert STRICT_PAGES <= pages


def test_ui_files_load_nothing_from_outside_the_local_server():
    """Links the user clicks (<a href>) may point anywhere: navigation is not
    governed by CSP. Resources (scripts, styles, images, media, frames, forms,
    fetch/EventSource/WebSocket targets) must be same-origin, data: or blob:."""
    seen = 0
    for f in ui_files():
        text = f.read_text(encoding="utf-8")
        if f.suffix == ".html":
            s = scan_html(f)
            assert not s.bad_tags, (f.name, s.bad_tags)
            assert s.javascript_urls == 0, f.name
            for tag, attr, val in s.loads:
                seen += 1
                assert not is_external(val), (f.name, tag, attr, val)
            for block in re.findall(r"<style[^>]*>(.*?)</style>", text, re.S | re.I):
                assert not _CSS_ABS.search(block), f.name
            for attr in re.findall(r"""\sstyle\s*=\s*"([^"]*)\"""", text):
                assert not _CSS_ABS.search(attr), f.name
        if f.suffix == ".css":
            assert not _CSS_ABS.search(text), f.name
        if f.suffix in (".html", ".js"):
            code = strip_js_comments(text) if f.suffix == ".js" else text
            assert not _JS_EVAL.search(code), f"{f.name}: eval-like call needs 'unsafe-eval'"
            assert not _JS_NET_ABS.search(code), f"{f.name}: request to an absolute URL"
            assert not _JS_SRC_ABS.search(code), f"{f.name}: .src to an absolute URL"
            for m in _JS_TAG_ABS.finditer(code):
                assert (f.name, m.group(0)) in KNOWN_FILE_AUDIO, f"{f.name}: resource tag pointing outside: {m.group(0)}"
    assert seen > 30   # the scan really looked at the pages


def test_strict_pages_and_their_scripts_have_no_inline_code():
    for name in sorted(STRICT_PAGES):
        page = WEB / name
        assert inline_markers(page) == (0, 0, 0, 0), name
        for js in local_scripts(page):
            assert inline_markers(js) == (0, 0, 0, 0), f"{name} -> {js.name}"
            assert not _JS_STYLE_ATTR.search(strip_js_comments(js.read_text(encoding="utf-8"))), js.name


def test_legacy_inline_code_does_not_grow_and_the_policy_covers_it():
    legacy = parse_csp(_server_policies()[0])
    strict_js = {js.name for n in STRICT_PAGES for js in local_scripts(WEB / n)}
    total = [0, 0, 0, 0]
    for f in ui_files():
        if f.suffix == ".css" or f.name in STRICT_PAGES or f.name in strict_js:
            continue
        got = inline_markers(f)
        allowed = LEGACY_INLINE.get(f.name, (0, 0, 0, 0))
        assert all(g <= a for g, a in zip(got, allowed)), \
            f"{f.name}: inline code grew ({got} > {allowed}). Use addEventListener / a CSS class instead"
        for i, g in enumerate(got):
            total[i] += g
    scripts_inline = total[0] + total[1] > 0
    styles_inline = total[2] + total[3] > 0
    # Covered: inline code exists => the policy allows it; no inline left => the allowance must go.
    assert ("'unsafe-inline'" in legacy["script-src"]) == scripts_inline
    assert ("'unsafe-inline'" in legacy["style-src"]) == styles_inline


# ── what the server really sends ───────────────────────────────────────────

@pytest.fixture()
def server(tmp_path, monkeypatch):
    dbp = tmp_path / "s.db"
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(dbp))
    from ops_dashboard import db
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    from ops_dashboard.server import DashboardHandler

    class Quiet(DashboardHandler):
        def log_message(self, *a):
            pass
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _headers(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.headers


def test_server_sends_the_legacy_policy_on_legacy_pages_and_strict_on_strict(server):
    legacy, stricts = _server_policies()
    checked = 0
    for path in ("/", "/ai", "/setup", "/boardroom", "/browser", "/tour", "/pair", "/media"):
        try:
            h = _headers(server + path)
        except Exception:      # a route that needs auth/app token in this setup
            continue
        assert h.get_all("Content-Security-Policy") == [legacy], path
        checked += 1
    assert checked >= 4
    h = _headers(server + "/")
    assert h.get_all("Content-Security-Policy") == [legacy]
    for path in ("/sessions", "/connectors", "/learning"):
        assert set(_headers(server + path).get_all("Content-Security-Policy")) <= stricts, path
