"""Guards the quick-capture inbox: add, list open, process, stats."""
from __future__ import annotations

import pytest


@pytest.fixture()
def cap(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "c.db"))
    import ops_dashboard.db as _db
    _db.DB_PATH = tmp_path / "c.db"  # type: ignore[attr-defined]
    _db.init_db()
    from ops_dashboard import capture
    return capture


def test_add_list_process(cap):
    cid = cap.add("read this later", kind="url", url="https://example.com")
    assert cid > 0
    open_ = cap.list_captures()
    assert len(open_) == 1 and open_[0]["kind"] == "url"
    assert cap.stats() == {"total": 1, "open": 1, "processed": 0}
    r = cap.process(cid)
    assert r["ok"] is True
    assert cap.list_captures() == []          # no longer open
    assert cap.stats()["processed"] == 1


def test_empty_content_rejected(cap):
    with pytest.raises(ValueError):
        cap.add("   ")


def test_process_missing(cap):
    assert cap.process(999)["ok"] is False
