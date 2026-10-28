"""Static guards for the first-run UI: strict page (nothing inline, nothing
remote), no HTML sinks, and the key never leaves the password field except in
the one POST that stores it."""
from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
SINKS = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|"
                   r"setTimeout\(\s*['\"]|setAttribute\(\s*['\"]on")


def _code(name: str) -> str:
    return re.sub(r"^\s*//.*$", "", (WEB / name).read_text(), flags=re.M)


def test_no_html_sinks_and_nothing_remote():
    for name in ("welcome.js", "first-run.js"):
        src = _code(name)
        assert not SINKS.search(src), name
        assert not re.search(r"https?://", src), name


def test_only_the_endpoints_it_needs():
    allowed = {"/api/onboarding/", "/api/library", "/api/credentials/", "/welcome"}
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
    assert src.count("field.value") == 2
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
    used = set(re.findall(r'data-i18n(?:-ph)?="([a-z0-9_]+)"', html))
    assert used and used <= es_keys, used - es_keys


def test_the_dashboard_checks_for_a_first_run_and_the_page_is_strict():
    index = (WEB / "index.html").read_text()
    assert '<script src="first-run.js" defer></script>' in index
    src = _code("first-run.js")
    assert "window.location.replace('/welcome')" in src and "s.fresh === true" in src
    assert "'/index.html'" in src  # only on the dashboard itself
