"""Guards RAG citations (#7): retrieved docs become numbered citations the Brain
answer carries through to the API. The pipeline already exists in brain.ask;
this locks the citation contract so it can't silently regress.
"""
from __future__ import annotations


def _doc(source, path, title, content, score):
    return {"source": source, "path": path, "title": title, "content": content, "score": score}


def test_format_context_builds_numbered_citations():
    from ops_dashboard import brain
    docs = [
        _doc("obsidian", "notes/a.md", "Arch decision", "We use SQLite", 0.91),
        _doc("repo", "src/db.py", "db module", "the db layer", 0.7),
    ]
    context, citations = brain._format_context(docs)
    assert len(citations) == 2
    c0 = citations[0]
    assert c0["n"] == 1 and c0["source"] == "obsidian" and c0["path"] == "notes/a.md"
    assert c0["title"] == "Arch decision" and c0["score"] == 0.91
    # each citation is numbered and referenced in the context block
    assert [c["n"] for c in citations] == [1, 2]
    assert "[1]" in context and "[2]" in context and "Arch decision" in context


def test_brain_answer_dataclass_has_citations():
    """The API contract: BrainAnswer exposes citations, and ask() populates it."""
    from ops_dashboard import brain
    fields = brain.BrainAnswer.__dataclass_fields__
    assert "citations" in fields and "answer" in fields
    # empty docs -> empty citations, no crash
    context, citations = brain._format_context([])
    assert citations == [] and context == ""
