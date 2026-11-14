"""Fallo 3 de los evals (#28): credentials in vault content must be redacted
before the content is sent to an LLM provider (Brain context, vault_search tool
results, note AI actions)."""
from __future__ import annotations

import pytest

from ops_dashboard import redact

# Built at runtime so no credential-shaped literal is committed.
FAKE = {
    "anthropic_key": "sk-ant-" + "api03-" + "x" * 30,
    "openai_key": "sk-" + "proj-" + "A1b2" * 10,
    "stripe_secret": "sk_" + "live_" + "Z9" * 12,
    "stripe_webhook_secret": "whsec_" + "q" * 24,
    "aws_access_key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "github_token": "ghp_" + "a" * 36,
    "slack_token": "xoxb-" + "1234567890-abcdef",
    "telegram_bot_token": "123456789:" + "A" * 35,
    "google_api_key": "AIza" + "B" * 35,
    "jwt": "eyJ" + "h" * 12 + ".eyJ" + "p" * 12 + "." + "s" * 12,
}


@pytest.mark.parametrize("kind", sorted(FAKE))
def test_known_credential_formats_are_redacted(kind):
    text = f"antes {FAKE[kind]} después"
    out, n = redact.redact_with_count(text)
    assert FAKE[kind] not in out and f"[REDACTED:{kind}]" in out
    assert out.startswith("antes ") and out.endswith(" después") and n == 1


def test_private_key_block_is_redacted_whole():
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
    out = redact.redact_secrets(f"mi clave:\n{pem}\nfin")
    assert "b3BlbnNzaC1rZXktdjEAAAA" not in out and "[REDACTED:private_key]" in out and out.endswith("fin")


def test_assignment_values_are_redacted_label_kept():
    out = redact.redact_secrets("password: hunter2hunter2hunter2\nAPI_KEY=\"abcd1234efgh5678\"\ncontraseña = Sup3rS3cr3tValue")
    assert "hunter2" not in out and "abcd1234efgh5678" not in out and "Sup3rS3cr3tValue" not in out
    assert "password: [REDACTED:credential]" in out and 'API_KEY="[REDACTED:credential]' in out
    assert "contraseña = [REDACTED:credential]" in out


@pytest.mark.parametrize("prose", [
    "El token de despliegue se pide al responsable de infraestructura.",
    "Password reset flow: send email, then expire after 1h.",
    "Ver https://docs.example.com/api-keys para crear una clave.",
    "Plan Pro a 29 € al mes; sk-ant- es el prefijo de Anthropic.",
    "",
])
def test_ordinary_prose_is_untouched(prose):
    assert redact.redact_with_count(prose) == (prose, 0)


def test_none_is_empty_string():
    assert redact.redact_secrets(None) == ""


def test_brain_context_is_redacted():
    from ops_dashboard import brain
    docs = [{"source": "obsidian", "path": "ops/deploy.md", "title": "Deploy",
             "content": "token viejo " + FAKE["github_token"], "score": 0.9}]
    context, citations = brain._format_context(docs)
    assert FAKE["github_token"] not in context and "[REDACTED:github_token]" in context
    assert citations[0]["path"] == "ops/deploy.md"


def test_vault_search_tool_redacts_excerpts(monkeypatch):
    from ops_dashboard import db, tools
    rows = [{"source": "obsidian", "title": "Deploy", "path": "ops/deploy.md",
             "excerpt": "token " + FAKE["stripe_secret"]}]
    monkeypatch.setattr(db, "search", lambda q, limit=10: rows)
    res = tools._impl_vault_search({"query": "token"})
    blob = repr(res)
    assert FAKE["stripe_secret"] not in blob and "[REDACTED:stripe_secret]" in blob


def test_note_ai_sends_redacted_content(monkeypatch, tmp_path):
    from ops_dashboard import brain, db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "redact.db")
    db.init_db()
    from ops_dashboard.indexers import note_ai
    sent: list[str] = []

    class P:
        name = "fake"
        model = "fake"

        def ask(self, system, messages, **kw):
            sent.append(messages[-1]["content"])
            return "ok"

    monkeypatch.setattr(note_ai, "get_note_content",
                        lambda doc_id: ({"id": 1, "source": "obsidian", "path": "a.md", "title": "A",
                                         "content": "clave " + FAKE["anthropic_key"]}, None))
    monkeypatch.setattr(brain, "resolve_provider", lambda *a, **k: P())
    action = next(iter(note_ai.ACTION_PROMPTS))
    note_ai.run_action(1, action)
    assert sent and FAKE["anthropic_key"] not in sent[0] and "[REDACTED:anthropic_key]" in sent[0]
