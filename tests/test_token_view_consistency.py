"""The token connector's view must not show a torn state.

``view()`` reports two independent things: ``last_sync`` (from the
connector's state file) and ``documents`` (counted in the documents table).
A sync writes the documents FIRST and the state file SECOND, so a view
request landing between the two reads returns ``documents=2`` with
``last_sync`` absent.

The UI renders both from that single payload, so the card shows "2 documents
in the vault" next to "never synced" — and the browser test that clicks
"Sync now" and reads the card fails on a slow machine, because it observes
the torn card instead of the synced one.

The invariant, stated so it can be checked: **the view must never report
documents it cannot attribute to a sync.** If there is no ``last_sync``, the
connector has not synced, so it cannot have documents either — whatever the
documents table says at that instant.
"""
from __future__ import annotations

from ops_dashboard.connectors_token import base


class _FakeConn:
    def __init__(self, **kw):
        self.id = kw.get("id", "fake")
        self.token_mode = kw.get("token_mode", "keychain")
        self.sync = kw.get("sync")
        self.config_fields = []
        self.token_help_es = ""
        self.token_help_en = ""
        self.spec = type("S", (), {"actions": {}})()


def _patch(monkeypatch, state: dict, documents: int):
    monkeypatch.setattr(base, "get", lambda cid: _FakeConn(id=cid))
    monkeypatch.setattr(base, "connections_for", lambda nc: [])
    monkeypatch.setattr(base, "config_of", lambda nc: {})
    monkeypatch.setattr(base, "load_state", lambda cid: dict(state))
    monkeypatch.setattr(base, "doc_count", lambda cid: documents)
    monkeypatch.setattr(base, "tokens",
                        type("T", (), {"load_token": staticmethod(lambda i: "secret")}))


def test_a_connector_that_never_synced_reports_no_documents(monkeypatch):
    """The torn window: the documents are in the table, the state file is not
    written yet. The view must not report documents it cannot attribute."""
    _patch(monkeypatch, state={}, documents=2)
    out = base.view("fake")
    assert out["last_sync"] is None
    assert out["documents"] == 0, out


def test_a_synced_connector_reports_its_documents(monkeypatch):
    _patch(monkeypatch, state={"last_sync": {"at": 1, "fetched": 2, "inserted_or_updated": 2}},
           documents=2)
    out = base.view("fake")
    assert out["last_sync"] is not None
    assert out["documents"] == 2, out


def test_forgetting_the_documents_clears_both(monkeypatch):
    """After "forget", the state file has no last_sync and the table is empty:
    the view reports neither."""
    _patch(monkeypatch, state={}, documents=0)
    out = base.view("fake")
    assert out["last_sync"] is None and out["documents"] == 0
