"""The shipped skill catalog must survive a frozen build.

The catalog is Markdown folders under ``ops_dashboard/skills/packs`` — a data
subdirectory that is NOT a Python package. ``collect_all("ops_dashboard")``
does not reliably pick that up, and the failure is silent: the app runs, the
pack list is empty, and nothing says why.

These tests pin the spec file so the datas entry cannot be dropped, and pin
the runtime resolution so the catalog is found wherever it was installed.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DESKTOP_SPEC = ROOT / "desktop" / "conductloop_server.spec"


def test_the_desktop_spec_ships_the_packs_directory():
    spec = DESKTOP_SPEC.read_text()
    assert "ops_dashboard/skills/packs" in spec, (
        "the skill catalog is a data subdirectory that collect_all() does not "
        "pick up; it must be listed explicitly in the spec's datas")


def test_the_spec_ships_the_whole_packs_tree_not_only_the_manifests():
    spec = DESKTOP_SPEC.read_text()
    # The destination is the path the runtime looks for.
    assert re.search(r"\(\s*str\([^)]*packs[^)]*\)\s*,\s*[\"']ops_dashboard/skills/packs[\"']\s*\)", spec)


def test_the_runtime_finds_the_catalog_inside_a_bundle():
    """The catalog path is derived from the module file, so it works both in
    a source checkout and inside a frozen bundle (where the module lives in
    a temp dir)."""
    from ops_dashboard.skills import packs
    resolved = packs.PACKS_DIR
    assert resolved.name == "packs"
    assert resolved.parent.name == "skills"


def test_the_catalog_is_discoverable_from_the_shipped_location():
    from ops_dashboard.skills import packs
    found = packs.discover_packs()
    assert found, "no packs found at the shipped location"
    ids = {p["id"] for p in found}
    assert len(ids) == 12, sorted(ids)


@pytest.mark.parametrize("role", [
    "cfo", "cmo", "cso", "cto", "general_counsel", "head_cs",
    "head_product", "head_sales", "vp_design", "vp_devops", "vp_frontend", "vp_mobile",
])
def test_every_c_suite_role_has_a_pack(role):
    from ops_dashboard.skills import packs
    found = {p["id"] for p in packs.discover_packs()}
    assert role in found
