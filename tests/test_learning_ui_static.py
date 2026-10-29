"""Static guards for the "Aprendizaje" UI (L3, D17): no HTML sinks for
server data, page wired from the dashboard, no third-party origins."""
from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
FILES = ["learning.js", "feedback-buttons.js"]
SINKS = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|"
                   r"setTimeout\(\s*['\"]|setAttribute\(\s*['\"]on")


def _code(name: str) -> str:
    src = (WEB / name).read_text()
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)  # header comments name the sinks


def test_no_html_sinks():
    for name in FILES:
        assert not SINKS.search(_code(name)), name


def test_only_learning_api_and_no_external_origins():
    for name in FILES:
        src = _code(name)
        assert not re.search(r"https?://", src), name
        for path in re.findall(r"['\"](/api/[a-z0-9_/\-]*)", src):
            assert path.startswith("/api/learning/"), (name, path)


def test_page_named_aprendizaje_and_linked():
    html = (WEB / "learning.html").read_text()
    assert "<title>Aprendizaje" in html and 'data-i18n="title">Aprendizaje<' in html
    assert "<script src=\"/learning.js\" defer></script>" in html
    assert not re.search(r"<script>(?!</script>)|\son[a-z]+=", html)  # CSP: no inline JS
    index = (WEB / "index.html").read_text()
    # The dashboard is English-only (i18n.js): the link says Learning; the page translates itself.
    assert 'href="/learning"' in index and "🎓 Learning<" in index
    assert '<script src="feedback-buttons.js" defer></script>' in index


def test_failure_log_copy_states_d6():
    js = (WEB / "learning.js").read_text()
    for needle in ("Desactivado por defecto", "30 días", "Off by default", "30 days"):
        assert needle in js


def test_skills_card_copy_states_d2_d13_d16():
    html = (WEB / "learning.html").read_text()
    assert 'id="skills-card"' in html and 'id="skills-external"' in html and 'id="trust-add"' in html
    js = (WEB / "learning.js").read_text()
    for needle in ("solo lectura", "read only", "Desactivado por defecto", "Off by default",
                   "nunca se descargan de Internet", "never downloaded from the Internet"):
        assert needle in js, needle


def test_lessons_card_d3_d4_d17():
    html = (WEB / "learning.html").read_text()
    for el_id in ("lessons-card", "lessons-enabled", "lessons-mirror", "lesson-add", "price-save"):
        assert f'id="{el_id}"' in html, el_id
    js = (WEB / "learning.js").read_text()
    assert "Lo que he aprendido" in js and "What I have learned" in js
    assert "Nada se activa solo" in js and "Nothing activates itself" in js


def test_skill_proposals_card_d3_d4_d14():
    html = (WEB / "learning.html").read_text()
    for el_id in ("skill-proposals-card", "sp-curator", "sp-create", "sp-kind", "sp-mode", "sp-list"):
        assert f'id="{el_id}"' in html, el_id
    js = (WEB / "learning.js").read_text()
    assert "El agente solo propone" in js and "The agent only proposes" in js
    assert "nunca se borran" in js and "never deleted" in js
    # proposal content and diffs are rendered as text only
    assert "el('pre', 'skill', p.diff)" in js and "ta.value = p.content" in js


def _i18n_keys(js: str, lang: str) -> set[str]:
    start = js.index(f"    {lang}: {{")
    end = js.index("\n    }", start)
    return set(re.findall(r"\b([a-z][a-z0-9_]*):\s*'", js[start:end]))


def test_l7_tabs_es_en_parity_and_csp_friendly_html():
    html = (WEB / "learning.html").read_text()
    tabs = re.findall(r'role="tab" id="tab-([a-z]+)" data-tab="\1"', html)
    assert tabs == ["lessons", "skills", "feedback", "failures", "evals", "metrics"]
    panels = set(re.findall(r'data-panel="([a-z]+)"', html))
    assert panels == set(tabs)
    assert 'id="evals-card"' in html and 'data-i18n="read_only"' in html
    assert not re.search(r"\sstyle=", html)  # style-src 'self': no inline styles either
    js = (WEB / "learning.js").read_text()
    es, en = _i18n_keys(js, "es"), _i18n_keys(js, "en")
    assert es == en, (sorted(es - en), sorted(en - es))
    used = set(re.findall(r'data-i18n(?:-ph)?="([a-z0-9_]+)"', html)) | set(re.findall(r"\bt\('([a-z0-9_]+)'", js))
    dynamic = {k for k in used if k.endswith("_")}
    assert not (used - dynamic - es), sorted(used - dynamic - es)
    assert "Lo que he aprendido" in js and "What I have learned" in js


def test_l7_linked_from_brain_hub_and_dashboard():
    assert 'href="/learning"' in (WEB / "brain.html").read_text()
    assert 'href="/learning"' in (WEB / "index.html").read_text()
