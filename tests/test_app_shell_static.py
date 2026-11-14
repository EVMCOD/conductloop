"""The app shell: one sidebar on every page, one appearance switch.

The sidebar is written once (web/app-nav.js) and every page that lives in the
app window loads it; light/dark follows the system or the person's choice on
every page (web/appearance.js). These checks keep a new page, view, word or
icon from falling out of it.
"""
import json
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
NAV = (WEB / "app-nav.js").read_text(encoding="utf-8")
# Pages that are not part of the app window (see STANDALONE in app-nav.js).
STANDALONE = {"welcome.html", "tour.html", "pair.html", "browser.html"}


def _entries(kind):
    return re.findall(r"\['" + kind + r"', '([^']+)', '([a-z_.]+)', '[^']*', '([a-z]+)'", NAV)


def _sprite():
    return set(re.findall(r"^\s*\['([a-z]+)', '<", NAV, re.M))


def test_every_page_loads_the_appearance_switch_first():
    for page in sorted(WEB.glob("*.html")):
        html = page.read_text(encoding="utf-8")
        body = html[html.index("<body"):]
        first = re.search(r"<script[^>]*>", body)
        assert first and 'src="/appearance.js"' in first.group(0), page.name


def test_every_app_page_carries_the_sidebar():
    for page in sorted(WEB.glob("*.html")):
        html = page.read_text(encoding="utf-8")
        has = '<script src="/app-nav.js"></script>' in html
        assert has == (page.name not in STANDALONE), page.name
    for name in STANDALONE:
        assert "'/" + name[:-5] + "'" in NAV.split("var STANDALONE")[1].split(";")[0], name


def test_every_link_is_a_page_and_every_view_is_rendered():
    pages = {p.stem for p in WEB.glob("*.html")}
    for href, _key, _icon in _entries("link"):
        assert href.strip("/").replace("/", "-") in pages, href
    app = (WEB / "app.js").read_text(encoding="utf-8")
    for view, _key, _icon in _entries("view"):
        assert "_view === '" + view + "'" in app, view


def test_pages_nothing_else_links_to_are_in_the_sidebar():
    hrefs = {h for h, _k, _i in _entries("link")}
    for path in ("/setup", "/connectors", "/channels", "/chat", "/sessions", "/jobs"):
        assert path in hrefs, path


def test_every_word_and_icon_of_the_sidebar_exists():
    words = {}
    for lang in ("en", "es"):
        words[lang] = json.loads((WEB / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
    keys = set(re.findall(r"'((?:sidebar|nav)\.[a-z_0-9]+)'", NAV))
    keys |= set(re.findall(r'data-i18n(?:-title|-label)?="([a-z_]+\.[a-z_0-9]+)"', NAV))
    assert len(keys) > 40
    for lang, d in words.items():
        for key in keys:
            section, name = key.split(".", 1)
            assert name in d.get(section, {}), f"{lang}: {key}"
    sprite = _sprite()
    used = {i for _h, _k, i in _entries("link")} | {i for _v, _k, i in _entries("view")}
    used |= set(re.findall(r"^\s*\['sidebar\.[a-z_]+', '[^']+', '([a-z]+)'", NAV, re.M))
    for name in ("index.html", "app.js"):
        used |= set(re.findall(r"#i-([a-z]+)", (WEB / name).read_text(encoding="utf-8")))
    used |= set(re.findall(r"treeIcon\('([a-z]+)'", (WEB / "app.js").read_text(encoding="utf-8")))
    assert used - {"' + name + '", "' + icon + '"} <= sprite, used - sprite


def test_the_dashboard_fills_its_sidebar_before_app_js_wires_it():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert '<aside class="activity-bar" id="activity-bar"></aside>' in html
    assert html.index('src="/app-nav.js"') < html.index('src="/app.js"')


def test_dark_tokens_reach_root_where_pages_alias_them():
    css = (WEB / "theme.css").read_text(encoding="utf-8")
    assert ":root:not(:has(> body.light)), body:not(.light) {" in css
    assert ":root:has(> body.dark), body.dark {" in css


def test_a_view_asked_from_another_page_is_not_replaced_by_now():
    # /?view=files must stay on Files even when the landing rule would open
    # "Now" (Files is also the default that rule replaces).
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "_viewAsked = true;" in app
    assert "&& !_currentProject && !_viewAsked)" in app


def test_fixed_bars_leave_room_for_the_sidebar():
    # Elements fixed to the left edge would sit under the sidebar (z-index 40).
    assert "body.has-app-nav .composer{left:var(--bar-w)}" in (WEB / "boardroom-thread.html").read_text(encoding="utf-8")
    assert "body.has-app-nav .rail { left: var(--bar-w); }" in (WEB / "chat.css").read_text(encoding="utf-8")
