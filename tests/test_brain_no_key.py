"""Without an AI key the brain answers with the lines of the retrieved notes
that match the question, instead of a placeholder that hides them.

The mock provider reads them from the context block that every prompt builder
writes with brain._format_context (brain.ask, /api/brain/stream, the chat)."""
from __future__ import annotations

from ops_dashboard import brain

MORTGAGE = {
    "source": "obsidian", "path": "/v/Daily/2026-10-20.md", "title": "2026-10-20",
    "content": "# 2026-10-20\nCalled the bank about the mortgage renewal: answer needed by 2026-11-05.\n"
               "Read 40 pages of a book.",
}
BOOK = {
    "source": "obsidian", "path": "/v/Books/Thinking.md", "title": "Thinking, Fast and Slow",
    "content": "---\ntags: [mortgage, book]\n---\n# Thinking, Fast and Slow\nSystem 1 is fast.\n"
               "Write the criteria before looking at the offers (mortgage!).",
}
MEETING = {
    "source": "obsidian", "path": "/v/Meetings/sync.md", "title": "Team sync",
    "content": "- Marta needs the design files by Friday.",
}


def _system(docs, *, tail=""):
    context, _ = brain._format_context(docs)
    return (brain.SYSTEM_PROMPT_BASE.format(today="2026-10-29", preferences="(none yet)")
            + "\n\nRelevant context (cite with [N]):\n" + context + tail)


def _ask(system, question):
    return brain.MockProvider().ask(system, [{"role": "user", "content": question}])


def test_shows_the_matching_line_with_its_citation_number():
    out = _ask(_system([MORTGAGE, BOOK]), "When do I need to answer the bank about the mortgage?")
    assert "**2026-10-20** [1]\n> Called the bank about the mortgage renewal: answer needed by 2026-11-05." in out
    assert "Read 40 pages" not in out
    assert "Credentials" in out


def test_a_single_common_word_is_not_a_match_next_to_a_strong_one():
    out = _ask(_system([MORTGAGE, MEETING]), "When do I need to answer the bank about the mortgage?")
    assert "Marta" not in out


def test_front_matter_heading_and_header_lines_are_never_quoted():
    out = _ask(_system([BOOK]), "mortgage offers criteria")
    assert "> Write the criteria before looking at the offers (mortgage!)." in out
    assert "tags:" not in out and "path:" not in out and "content:" not in out
    assert "> Thinking, Fast and Slow" not in out


def test_text_after_the_context_is_not_part_of_the_last_note():
    out = _ask(_system([MEETING], tail=brain.NO_CONTEXT_GUARD), "what did I write about my vault?")
    assert "Guardrails" not in out and "vault" not in out.split("matches")[0]


def test_web_clips_use_their_title_and_skip_the_markers():
    from ops_dashboard.web_ask import guard_web_docs
    clip = {"source": "web", "path": "https://example.com/a", "title": "Rates today",
            "content": "Mortgage rates fell to 3.0 % this week."}
    docs, rules = guard_web_docs([clip])
    out = _ask(_system(docs, tail=rules), "mortgage rates")
    assert "**Rates today** [1]\n> Mortgage rates fell to 3.0 % this week." in out
    assert "<<<" not in out and "url:" not in out and "security rules" not in out


def test_nothing_found_says_so_and_how_to_get_answers():
    out = _ask(_system([MEETING]), "What's the weather on Mars?")
    assert "nothing in your notes matches" in out and "Mars" in out
    assert "Credentials" in out


def test_no_context_block_at_all():
    out = _ask("You are a helpful assistant.", "hello there")
    assert "nothing in your notes matches" in out


def test_team_chat_context_block_is_read_too():
    from ops_dashboard import team_chat
    context, _ = brain._format_context([MORTGAGE])
    system = team_chat.agent_system({"name": "CFO", "title": "Chief Financial Officer"}, context, "")
    out = _ask(system, "mortgage bank answer")
    assert "[1]\n> Called the bank" in out
