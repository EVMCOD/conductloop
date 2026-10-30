"""Static guards for the dashboard's home without projects (app.js
renderNotesHome) and the question it hands to the chat."""
from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"


def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    for j in range(i, len(src)):
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[start:j + 1]
    raise AssertionError(name)


def test_the_home_shows_the_notes_only_without_projects_and_after_loading():
    main = _function((WEB / "app.js").read_text(), "renderMain")
    assert "!_projects.length && _allData.stats" in main
    assert "renderNotesHome()" in main


def test_server_text_is_never_written_as_html():
    body = _function((WEB / "app.js").read_text(), "renderNotesHome")
    for m in re.finditer(r"innerHTML\s*=\s*([^;]+);", body):
        assert re.fullmatch(r"''|'<span>[A-Za-z ]+</span>'", m.group(1).strip()), m.group(0)
    assert "insertAdjacentHTML" not in body and "outerHTML" not in body
    # No inline style or handlers: the CSP ratchet (test_csp_static) counts them.
    assert "style=" not in body and not re.search(r"['\"]\s*on[a-z]+=", body)


def test_it_only_talks_to_search_and_links_to_local_pages():
    body = _function((WEB / "app.js").read_text(), "renderNotesHome")
    assert set(re.findall(r"fetchJSON\('([^'?]+)", body)) == {"/api/find/recent", "/api/find"}
    assert set(re.findall(r"['\"](/[a-z]+)[/?'\"]", body)) <= {"/note", "/chat", "/setup", "/credentials", "/api"}


def test_a_question_from_the_url_is_put_in_the_box_never_sent():
    src = (WEB / "chat.js").read_text()
    i = src.index("params.get('q')")
    block = src[i:src.index("\n", src.index("if (ask", i))]
    assert "$('input').value = ask" in block
    assert "send(" not in block


def test_note_ai_actions_answer_in_the_browsers_language_not_always_spanish():
    html = (WEB / "note.html").read_text()
    for action in ("key_points", "summarize", "action_items", "expand"):
        assert f'data-action="{action}" data-lang="auto"' in html, action
    assert "const UI_LANG = /^es\\b/i.test(navigator.language || '') ? 'es' : 'en';" in html
    assert "b.dataset.lang || UI_LANG" in html
    # The Translate buttons keep their own target.
    assert 'data-action="translate" data-lang="es"' in html and 'data-action="translate" data-lang="en"' in html


def test_search_results_show_the_words_around_the_match():
    body = _function((WEB / "app.js").read_text(), "renderNotesHome")
    assert "r.content_snip || r.excerpt" in body
