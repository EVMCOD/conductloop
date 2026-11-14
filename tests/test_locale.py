"""The language the person chose.

The language is not ours to decide: each person picks it, and the app
remembers it (in the browser and in the app's configuration, so the server
speaks it too). These tests keep that promise honest.
"""
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

from ops_dashboard import brain, db, i18n, onboarding, team_chat

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A machine with nothing set up yet: no config, no notes, no key."""
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("OPS_CONFIG_PATH", str(cfg))
    monkeypatch.setattr(brain, "CONFIG_PATH", cfg)
    for var in ("OPS_VAULT_ROOT", "OPS_EXTRA_VAULTS", "OPS_WORKSPACE",
                "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MINIMAX_API_KEY", "GATEWAY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(db, "DEFAULT_VAULT", tmp_path / "default-vault")
    dbp = tmp_path / "ops.db"
    monkeypatch.setattr(db, "DB_PATH", dbp)
    db.init_db()
    return cfg


@pytest.fixture(autouse=True)
def _no_request_language():
    """The language of a request belongs to that request: no test inherits
    the one another test asked for."""
    i18n.set_request_language(None)
    yield
    i18n.set_request_language(None)


# ---------- the module ----------


def test_the_language_is_remembered(home):
    assert i18n.current_locale() == "en"      # the default
    i18n.set_locale("es")
    assert i18n.current_locale() == "es"
    # and it survives a new process: it is in the app's configuration
    assert json.loads(home.read_text())["locale"] == "es"


def test_only_the_languages_the_app_is_translated_into_are_accepted(home):
    assert i18n.SUPPORTED == ("en", "es")
    assert i18n.normalize("es-ES") == "es"
    assert i18n.normalize("ES_es") == "es"
    assert i18n.normalize("de") == "en"        # not translated (yet): the default
    assert i18n.normalize(None) == "en"
    assert i18n.normalize("") == "en"


def test_a_language_the_app_does_not_have_does_not_change_anything(home):
    i18n.set_locale("en")
    i18n.set_locale("de")
    assert i18n.current_locale() == "en"


def test_the_language_has_a_name_in_its_own_language():
    assert i18n.label("es") == "Español"
    assert i18n.label("en") == "English"


# ---------- the trap: choosing a language is not "setting the app up" ----------


def test_choosing_a_language_does_not_hide_the_welcome_page(home):
    """The welcome page is for someone who has just installed the app. If the
    language counted as a setting, picking it would hide the guide forever."""
    i18n.set_locale("es")
    state = onboarding.state()
    assert state["fresh"] is True
    assert state["documents"] == 0


# ---------- nobody has chosen yet ----------


def test_nobody_has_chosen_yet_the_browser_language_is_the_starting_point(home):
    """On a fresh install nobody has chosen a language, so the app starts in
    the language the browser asks for: a Spanish person gets a Spanish app
    without having to choose anything first."""
    assert i18n.chosen() is False
    i18n.set_request_language("es-ES,es;q=0.9")
    assert i18n.current_locale() == "es"
    i18n.set_request_language("de-DE,de;q=0.9")   # a language the app has not
    assert i18n.current_locale() == "en"
    i18n.set_request_language(None)
    assert i18n.current_locale() == "en"


def test_a_choice_the_person_made_wins_over_the_browser(home):
    """The chosen language is the app's: a browser that asks for another one
    does not get to change it."""
    i18n.set_locale("es")
    i18n.set_request_language("en-US,en;q=0.9")
    assert i18n.current_locale() == "es"
    assert i18n.chosen() is True


def test_a_language_the_app_does_not_have_is_not_a_choice(home):
    """A hand-edited configuration with a language we do not speak is not a
    choice: the browser's guess still starts the app."""
    home.write_text(json.dumps({"locale": "de"}), encoding="utf-8")
    assert i18n.chosen() is False
    i18n.set_request_language("es-ES")
    assert i18n.current_locale() == "es"


# ---------- the server speaks it too ----------


def test_the_agent_roles_are_in_the_language_the_person_chose(home):
    i18n.set_locale("en")
    en = {a["key"]: a for a in team_chat.roster()["agents"]}
    assert en["builtin:ceo"]["title"] == "Chief Executive Officer"
    assert en["brain"]["title"] == "Your second brain"

    i18n.set_locale("es")
    es = {a["key"]: a for a in team_chat.roster()["agents"]}
    assert es["builtin:ceo"]["title"] == "Director General"
    assert es["brain"]["title"] == "Tu segundo cerebro"
    # the names and the keys do not change: the team is the same team
    assert es["builtin:ceo"]["key"] == en["builtin:ceo"]["key"]
    assert es["builtin:ceo"]["name"] == en["builtin:ceo"]["name"]
    assert es["brain"]["name"] == en["brain"]["name"]


def test_the_answer_without_a_key_is_in_the_language_the_person_chose(home):
    from ops_dashboard import brain as b

    i18n.set_locale("es")
    out = b.MockProvider().ask("You are a helpful assistant.",
                                [{"role": "user", "content": "¿Qué es esto?"}])
    assert "No hay ninguna clave de IA" in out
    i18n.set_locale("en")
    out = b.MockProvider().ask("You are a helpful assistant.",
                                [{"role": "user", "content": "¿Qué es esto?"}])
    assert "No AI key is set up" in out


# ---------- over http ----------


@pytest.fixture()
def server(home):
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


def _call(base, method, path, body=None, language=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if language:
        headers["Accept-Language"] = language
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_the_language_can_be_read_and_chosen_over_http(server):
    status, body = _call(server, "GET", "/api/locale")
    assert status == 200
    assert body["locale"] == "en"
    assert body["supported"] == ["en", "es"]

    status, body = _call(server, "POST", "/api/locale", {"locale": "es"})
    assert status == 200
    assert body["locale"] == "es"
    assert i18n.current_locale() == "es"

    status, body = _call(server, "GET", "/api/locale")
    assert body["locale"] == "es"


def test_a_language_the_app_does_not_have_is_refused(server):
    status, body = _call(server, "POST", "/api/locale", {"locale": "de"})
    assert status == 400
    assert body["error"] == "unsupported_locale"
    assert i18n.current_locale() == "en"


def test_a_language_is_required(server):
    status, _ = _call(server, "POST", "/api/locale", {})
    assert status == 400


def test_the_server_says_whether_the_language_was_chosen(server):
    """The browser has to know the difference: a browser that only guessed
    must not overwrite the language somebody else chose."""
    status, body = _call(server, "GET", "/api/locale")
    assert status == 200
    assert body["chosen"] is False
    status, body = _call(server, "POST", "/api/locale", {"locale": "es"})
    assert body["chosen"] is True
    status, body = _call(server, "GET", "/api/locale")
    assert body["chosen"] is True
    assert body["locale"] == "es"


def test_the_server_speaks_the_language_the_request_asks_for(server):
    """Before anybody chooses, the server answers in the language of the
    request, so the page and the server say the same thing."""
    status, body = _call(server, "GET", "/api/locale", language="es-ES,es;q=0.9")
    assert status == 200
    assert body["locale"] == "es"
    assert body["chosen"] is False


def test_a_chosen_language_is_the_same_in_every_request(server):
    """A language somebody chose is the app's: it does not change with the
    browser that asks."""
    _call(server, "POST", "/api/locale", {"locale": "es"})
    status, body = _call(server, "GET", "/api/locale", language="en-US,en;q=0.9")
    assert body["locale"] == "es"
    assert body["chosen"] is True


# ---------- the browser side ----------


def test_the_browser_keeps_the_choice_instead_of_throwing_it_away():
    """The i18n loader used to delete a stored 'es' and always start in
    English: the language is the person's choice, so it is kept."""
    js = (WEB / "i18n.js").read_text()
    assert "const SUPPORTED = ['en', 'es'];" in js
    # a stored choice that the app has is kept and used; only one it does not
    # have is thrown away
    assert "if (stored && SUPPORTED.includes(stored))" in js
    # and it counts as a choice somebody made, not as a guess
    assert "_explicitChoice = true;" in js
    assert "localStorage.setItem(STORAGE_KEY, locale)" in js


def test_the_browser_only_follows_the_server_when_the_language_was_chosen():
    """The app's language wins over the browser's, but only when somebody
    chose it: a browser that merely guessed must not overwrite a choice."""
    js = (WEB / "i18n.js").read_text()
    assert "if (data.chosen)" in js
    assert "if (data.locale !== _currentLocale) setLocale(data.locale);" in js
    # a choice this browser made is offered to the server, so the roles and
    # the answers match the page
    assert "if (_explicitChoice && _currentLocale !== data.locale)" in js


def test_the_browser_tells_the_server_which_language_was_chosen():
    js = (WEB / "i18n.js").read_text()
    assert "'/api/locale'" in js
    assert "POST" in js


def test_every_page_that_offers_the_language_loads_the_translations():
    for page in ("index.html", "chat.html", "welcome.html"):
        html = (WEB / page).read_text()
        assert "i18n.js" in html, page


def test_the_chat_page_speaks_the_language_the_person_chose():
    """The chat is where the demo lands, so its words come from the
    dictionary and not from the source."""
    chat = json.loads((WEB / "i18n" / "chat.en.json").read_text())["chat"]
    es = json.loads((WEB / "i18n" / "chat.es.json").read_text())["chat"]
    assert set(chat) == set(es)
    js = (WEB / "chat.js").read_text()
    used = set(re.findall(r"t\(\s*'(chat\.[a-z0-9_]+)'", js))
    assert used, "the chat page must not hardcode its words"
    for key in used:
        assert key[len("chat."):] in chat, key


def test_the_two_dictionaries_agree_on_the_keys():
    en = json.loads((WEB / "i18n" / "en.json").read_text())
    es = json.loads((WEB / "i18n" / "es.json").read_text())

    def leaves(obj, prefix=""):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                yield from leaves(v, path)
            else:
                yield path, v

    a = dict(leaves(en))
    b = dict(leaves(es))
    assert list(a) == list(b), "same keys, same order"
    for key, value in a.items():
        # the placeholders have to survive the translation
        assert re.findall(r"\{(\w+)\}", value) == re.findall(r"\{(\w+)\}", b[key]), key


def test_the_chat_page_has_no_words_hardcoded_in_english():
    """The chat page used to write a few words of its own ('delete?', '1
    agent'), which stayed in English no matter what the person chose."""
    js = (WEB / "chat.js").read_text()
    assert "'delete?'" not in js
    assert '"delete?"' not in js
    assert "' agent'" not in js
    assert '" agent"' not in js
    assert "t('chat.delete_confirm')" in js
    assert "t('chat.delete_thread')" in js
    assert "t('chat.agent_one'" in js


def test_the_app_frame_speaks_the_chosen_language():
    """The frame of the app (the bar, the tree, the toolbar) is what every
    screen sits in: if it stays in English, choosing a language looks broken.
    The sidebar is written once, in app-nav.js, which the dashboard loads."""
    html = (WEB / "index.html").read_text()
    nav = (WEB / "app-nav.js").read_text()
    assert '<script src="/app-nav.js"></script>' in html
    for key in ("nav.chat", "nav.learning", "nav.credentials", "nav.media",
                "sidebar.costs", "sidebar.directory", "sidebar.gsc"):
        assert "'" + key + "'" in nav, key
    for key in ("common.filter_files", "common.search_palette"):
        assert key in html, key
    js = (WEB / "app.js").read_text()
    assert "tr('tree.to_do')" in js
    assert "tr('tree.sources')" in js


def test_every_word_the_frame_uses_exists_in_both_languages():
    en = json.loads((WEB / "i18n" / "en.json").read_text())
    es = json.loads((WEB / "i18n" / "es.json").read_text())

    def get(d, path):
        cur = d
        for part in path.split("."):
            cur = cur[part]
        return cur

    used = set(re.findall(r"tr\('([a-z0-9_.]+)'", (WEB / "app.js").read_text()))
    used |= set(re.findall(r'data-i18n(?:-ph|-title|-label)?="([a-z0-9_.]+)"',
                           (WEB / "index.html").read_text()))
    assert used
    for key in used:
        assert get(en, key), key
        assert get(es, key), key


def test_the_banners_follow_the_chosen_language_not_the_browser():
    """The banners used to ask the browser which language it was, so a
    Spanish app in an English browser answered in English."""
    for f in ("byok-banner.js", "provider-errors.js"):
        js = (WEB / f).read_text()
        assert "getLocale" in js, f
    byok = (WEB / "byok-banner.js").read_text()
    # the words are chosen when the banner is drawn, not when the file loads
    # (the app's language is still arriving from the server at that point)
    assert "function words()" in byok
    assert "var T = words();" in byok


# ---------- every page of the app ----------


def _page_dictionaries():
    """The pages that ask for a dictionary of their own, and the names."""
    out = {}
    for page in sorted(WEB.glob("*.html")):
        html = page.read_text(encoding="utf-8")
        if "i18n.js" not in html:
            continue
        m = re.search(r'data-i18n-pages="([^"]*)"', html)
        names = [n for n in re.split(r"[\s,]+", m.group(1)) if n] if m else []
        out[page.name] = names
    return out


def test_the_loader_keeps_its_names_to_itself():
    """A page with words of its own may declare a global t — the boardroom
    pages do. A redeclaration used to kill the loader, and the page stayed
    in English without saying why."""
    js = (WEB / "i18n.js").read_text()
    assert "(function () {" in js, "the loader must keep its names to itself"
    assert js.rstrip().endswith("})();")
    # nothing but _i18n goes into the window
    assert set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", js)) == {"_i18n"}


def test_every_page_that_speaks_has_its_dictionary():
    """A page that asks for a dictionary must have it, in both languages,
    with the same keys in the same order."""
    pages = _page_dictionaries()
    assert len(pages) >= 10, pages
    for page, names in pages.items():
        for name in names:
            en = WEB / "i18n" / f"{name}.en.json"
            es = WEB / "i18n" / f"{name}.es.json"
            assert en.exists(), (page, en.name)
            assert es.exists(), (page, es.name)
            a = json.loads(en.read_text(encoding="utf-8"))
            b = json.loads(es.read_text(encoding="utf-8"))
            assert list(a) == list(b), (page, name)
            for section, value in a.items():
                assert list(value) == list(b[section]), (page, name, section)
                for key, text in value.items():
                    assert re.findall(r"\{(\w+)\}", text) == \
                        re.findall(r"\{(\w+)\}", b[section][key]), (page, name, key)


def test_every_page_offers_the_language():
    """A page the person can land on must let them choose the language."""
    for page in sorted(WEB.glob("*.html")):
        name = page.name
        if name in ("index.html", "tour.html", "pair.html", "status.html"):
            continue  # the picker is mounted by the shell / not needed there
        html = page.read_text(encoding="utf-8")
        assert "i18n.js" in html, name


def test_every_word_a_page_uses_exists_in_its_dictionary():
    """A page that asks for a dictionary must not use a word it does not
    have: the page would show the key instead of the sentence.

    Only the pages that ask for a dictionary file are checked: the welcome
    page and a few others carry their own copy of the words, and the app's
    language is still the one they follow."""
    for page in sorted(WEB.glob("*.html")):
        html = page.read_text(encoding="utf-8")
        m = re.search(r'data-i18n-pages="([^"]*)"', html)
        names = [n for n in re.split(r"[\s,]+", m.group(1)) if n] if m else []
        if not names:
            continue
        served = set()
        # the main dictionary is always there; a page adds its own
        files = [WEB / "i18n" / f"{name}.en.json" for name in names]
        files.append(WEB / "i18n" / "en.json")
        for f in files:
            if not f.exists():
                continue
            for section, body in json.loads(f.read_text(encoding="utf-8")).items():
                served.update(f"{section}.{k}" for k in body)
        used = set(re.findall(r'data-i18n(?:-ph|-title|-label)?="([a-z0-9_.]+)"', html))
        js = page.with_suffix(".js")
        if js.exists():
            src = js.read_text(encoding="utf-8")
            used |= set(re.findall(r"_i18n\.t\(\s*'([a-z0-9_.]+)'", src))
        # the script of the page itself (inline) writes words too
        inline = "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                      html, re.S))
        used |= set(re.findall(r"(?<![\w.])t\(\s*['\"]([a-z0-9_.]+)['\"]", inline))
        used |= set(re.findall(r"(?<![\w.])tr\(\s*['\"]([a-z0-9_.]+)['\"]", inline))
        for key in used:
            assert key in served, (page.name, key)


def test_the_page_dictionaries_are_in_the_repository():
    """A dictionary that is not in the repository does not ship: the page
    shows its keys. The .gitignore once hid the credentials page's."""
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=30)
    if top.returncode != 0:
        pytest.skip("not a git checkout")
    listed = subprocess.run(["git", "ls-files", "ops_dashboard/web/i18n/"],
                            capture_output=True, text=True, timeout=30)
    tracked = {Path(line).name for line in listed.stdout.splitlines() if line.strip()}
    on_disk = {p.name for p in (WEB / "i18n").glob("*.json")}
    assert on_disk, "the dictionaries are missing altogether"
    assert on_disk <= tracked, sorted(on_disk - tracked)


def test_the_global_dictionary_owns_only_namespaced_keys():
    """A key without a namespace ("title") is found by accident in the flat
    fallback, and the word that lands there belongs to another page: the
    digest's title would end up as a heading."""
    for page in sorted(WEB.glob("*.html")):
        html = page.read_text(encoding="utf-8")
        used = set(re.findall(r'data-i18n(?:-ph|-title|-label)?="([a-z0-9_.]+)"', html))
        bare = sorted(k for k in used if "." not in k)
        assert not bare, (page.name, bare)


def test_the_welcome_page_has_words_of_its_own():
    """The first screen paints before anything has arrived, so it keeps its
    words close and marks them with its own attribute: the global dictionary
    must not overwrite them with a word of the same name."""
    html = (WEB / "welcome.html").read_text()
    assert "data-i18n" not in html, "the welcome page must not use the global attribute"
    assert "data-welcome=" in html
    js = (WEB / "welcome.js").read_text()
    assert "querySelectorAll('[data-welcome]')" in js
    # and its words are its own, in both languages
    assert "es: {" in js and "en: {" in js


def test_the_switcher_offers_every_language_the_app_has():
    js = (WEB / "locale-switch.js").read_text()
    assert "getSupported" in js
    assert "setLocale" in js


def test_the_selector_says_what_it_is_in_the_chosen_language():
    """The selector used to announce itself as "Language" even on a page
    written in Spanish."""
    en = json.loads((WEB / "i18n" / "en.json").read_text())
    es = json.loads((WEB / "i18n" / "es.json").read_text())
    assert en["common"]["language"] == "Language"
    assert es["common"]["language"] == "Idioma"

    js = (WEB / "locale-switch.js").read_text()
    assert "common.language" in js
    assert "label(el)" in js
    # the dashboard's own selector is translated the same way
    html = (WEB / "index.html").read_text()
    assert 'data-i18n-title="common.language"' in html
