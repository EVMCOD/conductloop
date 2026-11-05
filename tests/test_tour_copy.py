"""Guard: the quick tour describes the current product, not the retired one."""
import re
from pathlib import Path

TOUR = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web" / "tour.html"


def _text():
    return TOUR.read_text(encoding="utf-8")


def test_tour_does_not_promise_retired_features():
    text = _text()
    for phrase in ("GA4", "Sentry", "revenue", "Revenue", "earn", "13 projects", "traffic"):
        assert phrase not in text, phrase


def test_tour_has_no_invented_counts():
    assert not re.search(r"<strong>\s*\d+\s+\w+", _text())


def test_tour_covers_the_current_product():
    text = _text()
    for phrase in ("CEO", "BYOK", "connectors", "notes", 'href="/boardroom"', 'href="/connectors"', 'href="/credentials"'):
        assert phrase in text, phrase
