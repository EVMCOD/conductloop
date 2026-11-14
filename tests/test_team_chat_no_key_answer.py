"""Without an AI key the team chat answers with the mock provider: what it
quotes and matches must be what the person wrote, not the transcript wrapper
the team chat puts around it."""
from ops_dashboard import brain, i18n, team_chat


def _answer(history, message):
    text = team_chat._transcript(history, message)
    return brain.MockProvider().ask("", [{"role": "user", "content": text}])


def test_the_no_key_answer_quotes_the_message_not_the_wrapper():
    i18n.set_locale("en")
    try:
        out = _answer([{"role": "user", "content": "earlier question"}, {"role": "assistant", "content": "an answer"}],
                      "where is the launch plan")
        assert "“where is the launch plan”" in out
        assert "Latest message" not in out and "Conversation so far" not in out
    finally:
        i18n.set_locale("en")


def test_a_plain_message_is_quoted_as_is():
    i18n.set_locale("en")
    out = brain.MockProvider().ask("", [{"role": "user", "content": "hello there"}])
    assert "“hello there”" in out
