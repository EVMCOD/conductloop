"""The page has to say when the credential expires.

"Connect once" is true for a personal token and not for OAuth. The app
cannot change the provider's schedule, but it can stop being silent about
it: the card shows when the credential stops working, and it says so before
the break rather than the day the first sync fails.

The wording matters as much as the number:

- A credential with no expiry says it lasts until revoked. That is the good
  case, and it is what distinguishes a personal token from a consent.
- A credential with a date says how many days are left.
- A credential about to expire is marked, so it cannot be missed.
- The number comes from the server; the page only formats it.
"""
from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
JS = WEB / "connectors.js"


def _js() -> str:
    return JS.read_text(encoding="utf-8")


def test_the_page_reads_the_credential_state():
    src = _js()
    assert "credential" in src, "the page never looks at the credential state"


def test_the_page_shows_when_a_credential_expires():
    src = _js()
    assert "expiring_soon" in src
    assert "days_left" in src


def test_there_are_labels_for_the_three_states():
    src = _js()
    for key in ("cred_expires_in", "cred_expires_soon", "cred_no_expiry"):
        assert f"{key}:" in src, f"missing label {key}"


def test_the_labels_exist_in_both_languages():
    src = _js()
    # Each label appears twice: the Spanish block and the English block.
    for key in ("cred_expires_in", "cred_expires_soon", "cred_no_expiry"):
        assert src.count(f"{key}:") >= 2, f"{key} is missing from one language"


def test_the_expiry_is_only_shown_for_a_connected_connector():
    """A disconnected connector has no credential to talk about."""
    src = _js()
    block = src.split("function credentialLine(c)")[1].split("\n  function ")[0]
    assert "c.credential" in block
    assert "cred.connected" in block


def test_the_number_is_formatted_and_not_injected_raw():
    """The server sends the number; the page formats it. It must never be
    pasted into the page as markup."""
    src = _js()
    block = src.split("function credentialLine(c)")[1].split("\n  function ")[0]
    # No unescaped interpolation of the credential object.
    assert "${c.credential" not in block, "the credential object is interpolated raw"
    assert "${c.credential.days_left}" not in block, (
        "the days left are interpolated without escaping or formatting")


def test_the_page_does_not_show_a_raw_timestamp():
    """A unix timestamp is not something a human reads."""
    src = _js()
    block = src.split("function credentialLine(c)")[1].split("\n  function ")[0]
    assert "expires_at" not in block
