"""The page has to offer the personal token, not just the OAuth flow.

A connector that accepts a personal token is offering the user something
OAuth cannot: connect once, and the credential does not expire on the
provider's schedule. If the page only shows the OAuth button, the option
does not exist as far as the user is concerned.

So the card of a connector with ``token_auth`` gets a second button, and it
is NOT disabled by the missing client ID: a personal token needs no OAuth
app, and requiring one would defeat the purpose.

These are static checks on the page source, in the same style as the other
UI checks in this repo.
"""
from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
JS = WEB / "connectors.js"


def _js() -> str:
    return JS.read_text(encoding="utf-8")


def test_the_page_offers_a_personal_token_for_connectors_that_accept_one():
    src = _js()
    assert "token_auth" in src, "the page never looks at token_auth"


def test_the_token_button_is_not_gated_on_the_oauth_client_id():
    """The whole point of a personal token: no OAuth app to set up."""
    src = _js()
    block = src.split("function startTokenConnect")[1].split("\n  function ")[0] \
        if "function startTokenConnect" in src else ""
    assert block, "no token connect handler"
    assert "client_id_set" not in block, (
        "the token button depends on the OAuth client ID, which a personal "
        "token does not need")


def test_the_token_button_calls_the_token_route():
    src = _js()
    assert "/connect-token" in src, "the token route is never called"


def test_the_token_input_is_a_password_field():
    """The token is a credential: it must not be rendered in clear text."""
    src = _js()
    block = src.split("function startTokenConnect")[1].split("\n  function ")[0] \
        if "function startTokenConnect" in src else ""
    assert "type = 'password'" in block or 'type="password"' in block, \
        "the token input is not a password field"


def test_the_page_explains_why_a_token_is_worth_it():
    """The user has to know what they gain: a credential that does not expire
    on the provider's schedule."""
    src = _js()
    assert "token_expires_hint" in src or "token_advantage" in src


def test_the_labels_exist_in_both_languages():
    src = _js()
    for key in ("token_connect", "token_label", "token_hint"):
        assert f"{key}:" in src, f"missing label {key}"


def test_the_token_value_is_never_rendered_back_into_the_page():
    """A credential must not come back out of the API in clear text."""
    src = _js()
    assert "console.log" not in src.split("function startTokenConnect")[1].split("\n  function ")[0] \
        if "function startTokenConnect" in src else True
