"""The connectors page opens with a catalogue, not with a settings form.

The page used to be a list of connection settings: a client id, a set of
scopes, a credential line, an audit table. It answered "how do I wire this"
and never "what does this app do for me". Picking an app from that list was
guesswork.

Now it opens like a catalogue: a search box, the apps grouped in sections,
each with its mark, its name, one line saying what it does, and a button to
connect it. The settings move below, collapsed, where they belong — they are
still there and still work, they are just not the first thing you see.

These are static checks on the page source, in the same style as the other
UI checks in this repo.
"""
from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
HTML = WEB / "connectors.html"
JS = WEB / "connectors.js"
CSS = WEB / "connectors.css"


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def _js() -> str:
    return JS.read_text(encoding="utf-8")


def _css() -> str:
    return CSS.read_text(encoding="utf-8")


def test_the_page_has_a_catalogue_to_render_into():
    assert 'id="catalog-list"' in _html(), "there is no place to draw the catalogue"


def test_the_page_has_a_search_box():
    assert 'id="catalog-search"' in _html(), "you cannot look for an app"


def test_the_catalogue_comes_before_the_settings():
    """The catalogue is the first thing on the page. The settings are still
    there, below it — not removed, not first."""
    html = _html()
    catalog = html.index('id="catalog-card"')
    settings = html.index('id="connectors-card"')
    assert catalog < settings, (
        "the catalogue has to come before the connection settings, or the page "
        "still opens with a form")


def test_the_settings_are_collapsed_not_removed():
    """The client id, the scopes and the credential line are how a BYOK
    connection is made. They must stay reachable."""
    html = _html()
    assert "<details" in html, "the settings are not behind a disclosure"
    assert 'id="connectors-card"' in html, "the settings card is gone"


def test_the_catalogue_is_drawn_from_the_connector_list():
    src = _js()
    assert "renderCatalog" in src, "there is no catalogue renderer"
    assert "renderCatalog(data.connectors)" in src, (
        "the catalogue is not drawn from the connectors the API returns")


def test_each_tile_shows_the_name_and_what_it_does():
    src = _js()
    block = src.split("function renderCatalog")[1].split("\n  function ")[0]
    assert "description" in block, "the tile does not show the description"
    assert "description_es" in block, (
        "the tile has no Spanish description; the page is in Spanish by "
        "default")


def test_each_tile_has_a_connect_button():
    """The "+" of a catalogue. It is the way in."""
    src = _js()
    block = src.split("function renderCatalog")[1].split("\n  function ")[0]
    assert "data-testid" in block, "the tile has no testable connect button"
    assert "connect" in block.lower(), "the tile has no connect action"


def test_the_search_filters_the_tiles():
    src = _js()
    assert "catalog-search" in src, "the search box is never read"
    assert "filter" in src.lower(), "the search does not filter"


def test_the_tiles_are_a_grid():
    css = _css()
    assert ".catalog-grid" in css, "the catalogue is not a grid of tiles"
    assert "grid-template-columns" in css, "the grid does not lay out in columns"


def test_a_connected_app_says_so_instead_of_offering_to_connect():
    """A tile that still offers "+" for an app that is already connected
    makes the user think the connection did not take."""
    src = _js()
    block = src.split("function renderCatalog")[1].split("\n  function ")[0]
    assert "connected" in block, "the tile does not look at the connection state"


def test_the_section_names_are_translated():
    """The page is bilingual. The section names are UI text, so they live in
    the translation table, not hard-coded in the renderer."""
    src = _js()
    assert "catalog_section_" in src, "the section names are hard-coded"
