"""The CEO wizard's tool suggestions: generic built in, yours in config.json."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ops_dashboard import boardroom

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_CONFIG_PATH", str(tmp_path / "config.json"))

    def write(obj):
        (tmp_path / "config.json").write_text(json.dumps(obj), encoding="utf-8")

    return write


def test_without_config_only_the_generic_defaults_apply(config):
    base = boardroom.suggest_tools_for_project(None)
    assert base == boardroom._DEFAULT_TOOLS
    assert boardroom.suggest_tools_for_project("some-project") == base
    assert "schedule_chairman_brief" in boardroom.suggest_tools_for_project("holdco")


def test_projects_come_from_the_config(config):
    config({"boardroom": {"tool_defaults_by_project": {
        "acme": ["publish_blog_post", "monitor_signups"]}}})
    assert boardroom.suggest_tools_for_project("acme")[-2:] == ["publish_blog_post", "monitor_signups"]
    assert boardroom.suggest_tools_for_project("globex") == boardroom._DEFAULT_TOOLS


def test_the_config_wins_over_the_built_in_entry(config):
    config({"boardroom": {"tool_defaults_by_project": {"holdco": ["read_finance_report"]}}})
    assert boardroom.suggest_tools_for_project("holdco")[-1] == "read_finance_report"
    assert "schedule_chairman_brief" not in boardroom.suggest_tools_for_project("holdco")


@pytest.mark.parametrize("bad", [
    {"boardroom": "nope"}, {"boardroom": {"tool_defaults_by_project": ["x"]}},
    {"boardroom": {"tool_defaults_by_project": {"acme": "publish_blog_post"}}}, [], "text"])
def test_a_bad_config_is_ignored(config, bad):
    config(bad)
    assert boardroom.suggest_tools_for_project("acme") == boardroom._DEFAULT_TOOLS


def test_unknown_tools_and_values_of_the_wrong_type_are_dropped(config):
    config({"boardroom": {"tool_defaults_by_project": {"acme": ["publish_blog_post", "rm_rf", 3, None]}}})
    assert boardroom.suggest_tools_for_project("acme")[-1:] == ["publish_blog_post"]
    assert "rm_rf" not in boardroom.suggest_tools_for_project("acme")


def test_a_broken_config_file_is_ignored(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("OPS_CONFIG_PATH", str(tmp_path / "config.json"))
    assert boardroom.suggest_tools_for_project("acme") == boardroom._DEFAULT_TOOLS


@pytest.mark.parametrize("page", ["boardroom-create.html", "boardroom-autonomy.html"])
def test_every_category_of_the_catalog_is_shown_by_the_pages(page):
    """A tool whose category the page does not list would silently disappear."""
    html = (WEB / page).read_text(encoding="utf-8")
    listed = set(re.findall(r"'([a-z\-]+)'", re.search(r"const cats = \[(.*?)\];", html).group(1)))
    categories = {t["category"] for t in boardroom.all_tool_catalog()}
    assert categories <= listed, categories - listed
