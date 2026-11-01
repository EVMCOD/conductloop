"""Every app in the catalog says what it is for.

The connectors page was a list of connection settings: a client id, a set of
scopes, a credential line. It answered "how do I wire this" and never "what
does this app do for me" — so picking one was guesswork.

A catalog answers the second question. Each app carries a line that says
what it does, the way a store listing does, and a section it belongs to.

The guard at the bottom is the part that matters: a connector added later
without a description would otherwise ship as a blank tile, and nobody would
notice until a user asked what it was.
"""
from __future__ import annotations

import importlib
import pkgutil

import pytest

from ops_dashboard import connector_registry as registry


def _register_everything() -> None:
    """Register every connector, the way the app does.

    This runs at import time, not in a fixture: the parametrised tests read
    the registry while pytest is still collecting, and a fixture would not
    have run yet.
    """
    for pkg in ("connectors_native", "connectors_token"):
        mod = importlib.import_module(f"ops_dashboard.{pkg}")
        for m in pkgutil.iter_modules(mod.__path__):
            importlib.import_module(f"ops_dashboard.{pkg}.{m.name}")


_register_everything()


def _specs():
    return [s for s in registry.all_specs() if s.auth != "mcp"]


def test_there_are_connectors_to_describe():
    assert len(_specs()) >= 10, "the catalog is nearly empty; is this test even running?"


@pytest.mark.parametrize("spec", _specs(), ids=lambda s: s.id)
def test_every_connector_says_what_it_is_for(spec):
    """The line under the app's name.

    It has to say what the app does, not repeat the app's name: "Issues,
    pull requests and releases" for GitHub, not "GitHub connector".
    """
    assert spec.description.strip(), f"{spec.id} has no description"
    assert spec.description.strip() != spec.name.strip(), (
        f"{spec.id}'s description just repeats its name")


@pytest.mark.parametrize("spec", _specs(), ids=lambda s: s.id)
def test_every_connector_has_a_description_in_spanish(spec):
    """The page is in Spanish by default, and the English line is the
    fallback, not the main one."""
    assert spec.description_es.strip(), f"{spec.id} has no Spanish description"


@pytest.mark.parametrize("spec", _specs(), ids=lambda s: s.id)
def test_every_connector_belongs_to_a_section(spec):
    assert spec.category.strip(), f"{spec.id} has no category"
    assert spec.brand.strip(), f"{spec.id} has no brand colour"


@pytest.mark.parametrize("spec", _specs(), ids=lambda s: s.id)
def test_the_public_view_carries_the_catalog_fields(spec):
    """The page reads the API, not the registry."""
    pub = spec.public()
    for field in ("description", "description_es", "category", "brand"):
        assert field in pub, f"public() does not expose {field}"
        assert pub[field].strip(), f"public() exposes an empty {field}"


def test_the_brand_colour_is_a_colour():
    """A brand colour is a hex colour. A colour that does not parse would
    render as nothing and the tile would lose its identity."""
    for spec in _specs():
        assert spec.brand.startswith("#"), spec.id
        assert len(spec.brand) in (4, 7), f"{spec.id}: {spec.brand}"
        int(spec.brand[1:], 16)  # raises if it is not hex


def test_the_descriptions_are_not_boilerplate():
    """Eleven apps with the same sentence is a catalogue that says nothing."""
    lines = {s.description.strip().lower() for s in _specs()}
    assert len(lines) == len(_specs()), "two connectors share the same description"
