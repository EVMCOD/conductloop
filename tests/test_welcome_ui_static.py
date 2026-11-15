"""Static guards for the first-run UI: strict page (nothing inline, nothing
remote), no HTML sinks, and the key never leaves the password field except in
the one POST that stores it."""
from __future__ import annotations

import re
from html import unescape
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
SINKS = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|"
                   r"setTimeout\(\s*['\"]|setAttribute\(\s*['\"]on")


def _code(name: str) -> str:
    return re.sub(r"^\s*//.*$", "", (WEB / name).read_text(), flags=re.M)


def _dictionary(name: str) -> dict[str, str]:
    """The {key: 'text'} pairs of one dictionary in welcome.js."""
    js = _code("welcome.js")
    start = js.index(f"    {name}: {{")
    end = js.index("\n    },", start)
    out: dict[str, str] = {}
    for m in re.finditer(r"\b([a-z][a-z0-9_]*):\s*'((?:[^'\\]|\\.)*)'", js[start:end]):
        out[m.group(1)] = m.group(2).replace("\\'", "'")
    return out


def _page_text() -> dict[str, str]:
    """The text the page paints per key: the element text and the placeholder."""
    html = (WEB / "welcome.html").read_text()
    out: dict[str, str] = {}
    for m in re.finditer(r'data-welcome="([a-z0-9_]+)"[^>]*>([^<]*)<', html):
        out[m.group(1)] = unescape(m.group(2)).strip()
    for m in re.finditer(r"<input[^>]*data-welcome-ph=\"([a-z0-9_]+)\"[^>]*>", html):
        ph = re.search(r'placeholder="([^"]*)"', m.group(0))
        out[m.group(1)] = unescape(ph.group(1)).strip() if ph else ""
    return out


def test_no_html_sinks_and_nothing_remote():
    for name in ("welcome.js", "first-run.js"):
        src = _code(name)
        assert not SINKS.search(src), name
        assert not re.search(r"https?://", src), name


def test_only_the_endpoints_it_needs():
    allowed = {"/api/onboarding/", "/api/library", "/api/credentials", "/api/credentials/", "/welcome"}
    for name in ("welcome.js", "first-run.js"):
        for path in re.findall(r"['\"](/(?:api|welcome)[a-z0-9_/.\-]*)", _code(name)):
            assert any(path.startswith(a) for a in allowed), (name, path)


def test_the_page_has_no_inline_code_and_loads_only_local_files():
    html = (WEB / "welcome.html").read_text()
    assert not re.search(r"<script>(?!</script>)|\son[a-z]+=|\sstyle=|javascript:", html)
    assert not re.search(r"(?:src|href)=[\"']https?:", html)
    assert '<script src="/welcome.js" defer></script>' in html
    assert "<form" not in html  # the strict policy has form-action 'none'


def test_the_key_field_is_a_password_field_that_is_not_remembered():
    html = (WEB / "welcome.html").read_text()
    field = re.search(r'<input id="api-key"[^>]*>', html).group(0)
    assert 'type="password"' in field and 'autocomplete="off"' in field and "value=" not in field


def test_the_key_is_never_logged_stored_or_put_in_a_url():
    src = _code("welcome.js")
    for forbidden in ("console.", "localStorage", "sessionStorage", "document.cookie =", "history.", "location.search",
                      "location.hash", "window.name"):
        assert forbidden not in src, forbidden
    # the field is emptied right after the request is built, and the only use of
    # its value is the body of the POST
    assert "field.value = ''" in src
    # api-key field: read once, clear once. passphrase field (when shown) does the same.
    assert src.count("field.value") >= 2
    assert "{ value: value }" in src
    assert "'/api/credentials/byok.' + encodeURIComponent(provider) + '.default'" in src
    # what is shown when saving fails never comes from the server's text
    key_part = src[src.index("function saveKey"):src.index("function leave")]
    assert "message" not in key_part and "textContent" not in key_part.replace("say(", "")


def test_every_text_in_the_page_has_a_spanish_version():
    html = (WEB / "welcome.html").read_text()
    js = (WEB / "welcome.js").read_text()
    start = js.index("    es: {")
    end = js.index("\n    },", start)
    es_keys = set(re.findall(r"\b([a-z][a-z0-9_]*):\s*'", js[start:end]))
    # The welcome page has words of its own (it paints before anything has
    # arrived), so it marks them with its own attribute: the global
    # dictionary must not overwrite them with a word of the same name.
    used = set(re.findall(r'data-welcome(?:-ph)?="([a-z0-9_]+)"', html))
    assert used and used <= es_keys, used - es_keys
    assert "data-i18n" not in html, "the welcome page must not use the global attribute"


def test_the_two_dictionaries_offer_the_same_keys():
    """English and Spanish must offer the same strings: a key that exists in
    one and not in the other shows up as the other language in the page."""
    es = _dictionary("es")
    en = _dictionary("en")
    assert set(es) == set(en), set(es) ^ set(en)


def test_the_english_fallback_in_the_page_matches_the_english_dictionary():
    """The page paints in English before welcome.js arrives, so the English
    text also lives in welcome.html. The two copies must not drift apart."""
    en = _dictionary("en")
    page = _page_text()
    assert page, "the page has no data-welcome text"
    for key, text in page.items():
        assert key in en, key
        assert text == en[key], (key, text, en[key])


def test_the_welcome_page_does_not_promise_that_nothing_leaves_the_computer():
    """The product is BYOK and it can write outside: the page says what
    actually happens instead of "nothing leaves your computer"."""
    html = (WEB / "welcome.html").read_text().lower()
    assert "nothing leaves your computer" not in html
    assert "no cloud" not in html
    # what is true is said plainly
    assert "provider" in html          # the question goes to the provider you chose
    assert "confirmation" in html      # a connector write waits for it
    assert "pair a phone" in html      # the local network is a switch you control
    # and the promise that is true is kept
    assert "no conductloop account" in html
    assert "no conductloop cloud" in html
    assert "no subscription" in html


def test_the_spanish_copy_says_what_the_english_copy_says():
    """The Spanish page must carry the same truth as the English one."""
    text = " ".join(_dictionary("es").values()).lower()
    assert "nada sale de tu ordenador" not in text
    assert "proveedor" in text
    assert "confirmación" in text
    assert "móvil" in text
    assert "ni cuenta de conductloop" in text
    assert "ni suscripción" in text


def test_the_dashboard_checks_for_a_first_run_and_the_page_is_strict():
    index = (WEB / "index.html").read_text()
    assert '<script src="first-run.js" defer></script>' in index
    src = _code("first-run.js")
    assert "window.location.replace('/welcome')" in src and "s.fresh === true" in src
    assert "'/index.html'" in src  # only on the dashboard itself


def test_the_welcome_page_says_what_the_app_is_before_it_asks_for_anything():
    """The first screen is the product, not a form: the value comes first and
    the two things to configure are behind it."""
    html = (WEB / "welcome.html").read_text()
    assert 'id="screen-intro"' in html and 'id="screen-setup"' in html
    assert 'id="screen-setup"' in html and 'hidden' in html
    assert 'id="try-demo"' in html and 'id="setup"' in html
    js = (WEB / "welcome.js").read_text()
    assert "showScreen" in js and "tryDemo" in js


def test_the_sample_data_is_only_offered_into_an_empty_workspace():
    """The button is hidden once there are real notes, and the sample is
    removed when the user indexes their own."""
    js = (WEB / "welcome.js").read_text()
    assert "real_documents" in js
    assert "$('try-demo').hidden = true" in js
    assert "removeSample" in js


def test_the_dashboard_says_when_the_workspace_is_showing_sample_data():
    """Otherwise the sample notes look like the user's own."""
    index = (WEB / "index.html").read_text()
    assert '<script src="demo-banner.js"></script>' in index
    js = (WEB / "demo-banner.js").read_text()
    assert "/api/onboarding/demo/clear" in js
    assert "st.demo" in js


def test_the_sample_banner_only_talks_to_the_onboarding_endpoints():
    src = (WEB / "demo-banner.js").read_text()
    assert not SINKS.search(src)
    assert not re.search(r"https?://", src)
    for path in re.findall(r"['\"](/(?:api|welcome)[a-z0-9_/.\-]*)", src):
        # the only page it opens is the welcome guide, and the only API it
        # calls is the one that takes the sample away
        assert path.startswith("/api/onboarding/") or path == "/welcome", path


def test_the_banner_sends_the_user_to_the_brain_with_a_question():
    """The demo is about reading the notes, so it goes to the Brain — not to
    the team lead, who would split the question across the whole company."""
    js = (WEB / "demo-banner.js").read_text()
    assert "/chat?agent=brain&q=" in js
    assert "builtin:ceo" not in js


def test_the_banner_speaks_the_language_the_person_chose():
    """The app speaks the language of the app — the one the person chose —
    not the language of the sample data or of the browser looking at it."""
    js = (WEB / "demo-banner.js").read_text()
    assert "window._i18n.getLocale" in js
    assert "lang: chosenLocale()" in (WEB / "welcome.js").read_text()
