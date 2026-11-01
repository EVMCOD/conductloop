"""Every path that answers a human must reach the connectors.

This file is the guard against the gap coming back. It happened once: the
connector tools were built, the Brain and the orchestrator used them, and the
Boardroom — where the twelve CEOs live, the ones that have role playbooks —
called the provider with no tools at all. The feature was built to make the
agents useful and it landed on the one surface where the agents do not use
it.

So the paths are enumerated here, and a new one that answers a human has to
be added to this list. A path that is not in the list is a path that does
not reach the connectors.

The list is deliberately explicit. An automatic scan for "whoever calls
provider.ask" cannot tell a background summariser (which has no human in the
loop and needs no connector) from the CEO answering the chairman (which
does). That distinction is the whole point of this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "ops_dashboard"


# The paths where a human is waiting for the answer.
HUMAN_PATHS = {
    "brain.ask": "brain.py",
    "brain.stream": "brain.py",
    "boardroom.generate_ceo_reply": "boardroom.py",
    "boardroom.chat_with_holding_ceo": "boardroom.py",
    "team_chat._stream_answer": "team_chat.py",
    "orchestrator.run": "orchestrator.py",
    "server._brain_ask_tools": "server.py",
}

# The paths that are NOT conversations, declared so their absence is a
# decision rather than an oversight.
#
# The session agents (the one that works in a repo and the one that drives the
# browser) run their own tool loop with their own protocol (CodeTools: list,
# read, search, write). Folding the connectors into that loop is a different
# piece of work — it is not that they cannot use a connector, it is that
# they have a different machinery for tools.
NOT_CONVERSATIONS = {
    "runner.py",         # sessions/runner.py: the session agent's own tool loop
    "browser_runner.py", # sessions/browser_runner.py: same
}


@pytest.mark.parametrize("path_name,filename", sorted(HUMAN_PATHS.items()))
def test_the_path_is_reachable_and_real(path_name, filename):
    """Every listed path exists in the file it claims."""
    src = (APP / filename).read_text(encoding="utf-8")
    name = path_name.split(".")[-1]
    assert name in src, f"{path_name} is not in {filename}"


@pytest.mark.parametrize("path_name,filename", sorted(HUMAN_PATHS.items()))
def test_the_path_offers_the_connectors(path_name, filename):
    """And every one of them can reach a connector.

    Either it offers the tools, or it goes through the shared helper that
    does. There is no third way: a path that answers a human and cannot
    reach the user's own apps is a path where the agent has to guess.
    """
    src = (APP / filename).read_text(encoding="utf-8")
    offers = ("ask_with_connector_tools" in src
              or "ask_with_tools" in src
              or "prompt_block" in src)
    assert offers, (
        f"{path_name} ({filename}) answers a human but never offers the "
        f"connectors. Either it offers them or it goes through "
        f"brain.ask_with_connector_tools.")


def test_the_shared_helper_is_the_one_place_that_adds_the_block():
    """One helper, so the block cannot drift between paths."""
    brain = (APP / "brain.py").read_text(encoding="utf-8")
    assert "def ask_with_connector_tools" in brain
    # And the block itself is built in one place.
    connector_tools = (APP / "connector_tools.py").read_text(encoding="utf-8")
    assert "def prompt_block" in connector_tools


def test_the_background_paths_are_not_in_the_list():
    """The indexers summarise notes in the background. They have no human
    waiting, so they need no connector — and they are NOT in the list, on
    purpose. If someone adds them, this test should make them think."""
    ai_summary = (APP / "indexers" / "ai_summary.py").read_text(encoding="utf-8")
    assert "provider.ask(" in ai_summary
    assert "ai_summary" not in " ".join(HUMAN_PATHS)


def test_a_new_human_path_has_to_be_declared_here():
    """The guard against the gap coming back.

    Every module that calls the provider has to be either in the list (a
    human is waiting) or an indexer (nobody is). A module that does
    neither is a path nobody checked.
    """
    declared = set(HUMAN_PATHS.values())
    indexers = {"ai_summary.py", "note_ai.py"}
    callers = set()
    for path in APP.rglob("*.py"):
        if "tests" in path.parts or path.name.startswith("test_"):
            continue
        src = path.read_text(encoding="utf-8", errors="ignore")
        if "provider.ask(" in src or ".ask_with_tools(" in src:
            callers.add(path.name)
    undeclared = (callers - declared - indexers - NOT_CONVERSATIONS - {
        # the shared plumbing itself, and the eval harness
        "brain.py", "evals.py",
    })
    assert not undeclared, (
        f"these modules answer with a provider but are not declared as human "
        f"paths: {sorted(undeclared)}. Add them to HUMAN_PATHS if a human is "
        f"waiting, or to the indexer list if nobody is.")
