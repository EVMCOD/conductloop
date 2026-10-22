"""Inline-style invisibility filter in web_clip (the JS layer is in
test_browser_visibility_js.py)."""
import pytest

from ops_dashboard import web_clip


@pytest.mark.parametrize("html,reason", [
    ('<p style="opacity:0">X</p>', "opacity"),
    ('<p style="opacity: 0.01">X</p>', "opacity"),
    ('<p style="position:absolute;clip:rect(0 0 0 0)">X</p>', "clip"),
    ('<p style="clip-path:inset(50%)">X</p>', "clip_path"),
    ('<p style="clip-path:circle(0)">X</p>', "clip_path"),
    ('<div style="width:0;height:0;overflow:hidden">X</div>', "size"),
    ('<p style="position:absolute;left:-9999px">X</p>', "offscreen"),
    ('<p style="color:transparent">X</p>', "color"),
    ('<p style="color:#fff">X</p>', "color"),
    ('<div style="background:#123456"><p style="color:#123457">X</p></div>', "color"),
    ('<div style="color:white"><p>X</p></div>', "color"),
    ('<p style="font-size:0">X</p>', "font_size"),
    ('<div style="display:none"><p>X</p></div>', "display"),
    ('<div style="visibility:hidden"><p>X</p></div>', "visibility"),
    ('<div aria-hidden="true"><p>X</p></div>', "aria_hidden"),
    ('<p hidden>X</p>', "hidden_attr"),
])
def test_hidden_text_is_dropped_and_counted(html, reason):
    md, counts = web_clip.html_to_markdown_counted(f"<p>keep</p>{html}")
    assert "X" not in md and "keep" in md
    assert counts.get(reason) == 1, counts


@pytest.mark.parametrize("html", [
    '<div style="background:#000"><p style="color:#fff">X</p></div>',
    '<div style="font-size:0"><span style="font-size:16px">X</span></div>',
    '<div style="background:url(a.png)"><p style="color:#fff">X</p></div>',
    '<p style="opacity:0.5">X</p>',
    '<p style="color:#333">X</p>',
])
def test_visible_text_is_kept(html):
    md, counts = web_clip.html_to_markdown_counted(html)
    assert "X" in md and not counts


def test_summary_merges_page_and_backend_counts_and_clamps():
    page = web_clip._page_hidden({"total": 10**9, "reasons": {"offscreen": 10**9, "Bogus\n": 3, "x": "1"}})
    assert page == {"offscreen": 100_000}
    out = web_clip.hidden_summary(page, {"color": 2})
    assert out == {"total": 100_002, "reasons": {"offscreen": 100_000, "color": 2},
                   "page": 100_000, "backend": 2}


def test_clip_metadata_carries_the_count():
    clip = web_clip.build_clip({"url": "https://a.example/", "title": "t",
                                "html": '<p>keep</p><p style="opacity:0">X</p>',
                                "hidden": {"total": 1, "reasons": {"offscreen": 1}}})
    hd = clip["metadata"]["hidden_dropped"]
    assert hd["total"] == 2 and hd["reasons"] == {"offscreen": 1, "opacity": 1}


def test_agent_snapshot_notes_the_count_without_the_text():
    from ops_dashboard.sessions.browser import format_snapshot
    out, _ = format_snapshot({"url": "https://a.example/", "title": "t", "text": "hi", "elements": [],
                           "hidden": {"total": 3, "reasons": {"offscreen": 2, "color": 1}}})
    assert "3 invisible element(s)" in out
    assert "invisible" not in format_snapshot({"url": "https://a.example/", "title": "t",
                                               "text": "hi", "elements": []})[0]
