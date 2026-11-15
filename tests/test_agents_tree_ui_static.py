"""The tree of agents can be walked with the keyboard, found by name, and
edited from the path above it.

The browser test (tests/test_agents_tree_ui_browser.py) drives all of it;
this one is the cheap guard that keeps the pieces in place, and it says
what each one is for:

* the canvas is a tree that takes the focus, and the arrows walk it;
* there is a field to go to an agent by name;
* in edit mode, a name of the path takes a dropped member (that boss);
* "New agent" is a form with the choices, not three questions typed by
  hand.

(2026-11-15, prompt-minimax-2026-11-15 P1-1, P1-4)
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "ops_dashboard" / "web"
APP = (WEB / "app.js").read_text(encoding="utf-8")
NEW_AGENT = (WEB / "agent-new.js").read_text(encoding="utf-8")


def test_the_canvas_is_a_tree_that_takes_the_focus():
    assert 'role="tree"' in APP
    assert "tabindex=\"0\"" in APP, "the tree cannot be reached with the keyboard"
    assert 'id="org-canvas"' in APP


def test_the_arrows_walk_the_tree():
    handler = APP[APP.index("canvas.addEventListener('keydown'"):]
    handler = handler[:handler.index("});", handler.index("canvas.addEventListener('keydown'"))]
    for key in ("ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Enter", "Escape"):
        assert key in handler, f"the tree does not answer {key}"


def test_escape_closes_the_card_and_goes_back_to_the_whole_organisation():
    i = APP.index("canvas.addEventListener('keydown'")
    handler = APP[i:i + 4000]
    escape = handler[handler.index("'Escape'"):][:600]
    assert "_agentSelectedId = null" in escape, escape
    assert "_agentSelectedMember = null" in escape, escape
    # The key must not reach the document: its handler navigates back.
    assert "stopPropagation" in escape, escape


def test_there_is_a_field_to_go_to_an_agent_by_name():
    assert 'id="agent-search"' in APP
    assert "agent-search-results" in APP
    # The query lives outside the DOM: the panel is re-rendered when the
    # data arrives, and what was typed must not be lost with it.
    assert "_agentSearchQuery" in APP
    # Choosing somebody from the list does what pressing Enter does.
    assert "selectNode(parseInt(first.dataset.searchMemberId))" in APP


def test_a_dropped_member_takes_the_boss_of_the_path():
    i = APP.index("mount.querySelectorAll('[data-org-focus]')")
    block = APP[i:i + 2000]
    assert "dragover" in block and "drop" in block, block
    assert "window._agentDrop(target, e)" in block, block
    # And the question names the people, not their ids.
    assert '[data-org-focus="' in APP


def test_the_new_agent_form_asks_the_choices_instead_of_a_number():
    assert "team.id = 'agent-new-team'" in NEW_AGENT
    assert "type.id = 'agent-new-type'" in NEW_AGENT
    assert "boss.id = 'agent-new-boss'" in NEW_AGENT
    assert "name.id = 'agent-new-name'" in NEW_AGENT
    # The team comes from the teams that exist, and the boss from the
    # people of the organisation.
    assert "opts.teams" in NEW_AGENT and "opts.members" in NEW_AGENT
    # The dialog is a <dialog> of the page, like the one confirm.js opens.
    assert "showModal()" in NEW_AGENT
    # It is loaded by the page that uses it.
    index = (WEB / "index.html").read_text(encoding="utf-8")
    assert "agent-new.js" in index


def test_the_server_accepts_the_boss_of_a_new_agent():
    server = (ROOT / "ops_dashboard" / "server.py").read_text(encoding="utf-8")
    i = server.index("def _members_create")
    block = server[i:i + 1500]
    assert "manager_id" in block, block
    assert "db.add_member(" in block and "manager_id=manager" in block


def test_the_labels_of_the_dialog_are_in_the_dictionary_of_the_page():
    for code in ("en", "es"):
        d = json.loads((WEB / "i18n" / f"{code}.json").read_text(encoding="utf-8"))
        dash = d["dashboard"]
        for key in ("dlg_confirm", "dlg_cancel", "dlg_ok"):
            assert dash.get(key), f"{code}: dashboard.{key}"
        for key in ("agent_name_label", "agent_team_label", "agent_type_label",
                    "agent_boss_label", "agent_create", "agent_search_ph"):
            assert dash.get(key), f"{code}: dashboard.{key}"


def test_the_keys_of_both_dictionaries_are_the_same():
    en = json.loads((WEB / "i18n" / "en.json").read_text(encoding="utf-8"))
    es = json.loads((WEB / "i18n" / "es.json").read_text(encoding="utf-8"))

    def keys(d, prefix=""):
        out = set()
        for k, v in d.items():
            if isinstance(v, dict):
                out |= keys(v, prefix + k + ".")
            else:
                out.add(prefix + k)
        return out

    assert keys(en) == keys(es), sorted(keys(en) ^ keys(es))[:20]
