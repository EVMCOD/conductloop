"""Redact credentials from local content before it is sent to an LLM provider.

The vault is personal text: notes routinely contain pasted API keys, tokens or
private keys. Anything we put in a prompt leaves the Mac (to the provider the
user chose), so vault content is passed through :func:`redact_secrets` first.

Redaction keeps the surrounding text and replaces the credential with
``[REDACTED:<kind>]`` so the model can still say "there is a token in that note"
without ever seeing it. Patterns are conservative (real key prefixes + minimum
length, PEM private-key blocks, and ``key/token/password: <long value>``
assignments) to avoid mangling ordinary prose.

Never log the matched values; callers only get the redacted text and a count.
"""
from __future__ import annotations

import re

_PEM_BLOCK = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)

# (kind, pattern). Order matters: more specific prefixes first.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,}")),
    ("stripe_secret", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("stripe_webhook_secret", re.compile(r"\bwhsec_[A-Za-z0-9]{16,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
)

# "api_key: abc…", "password = …", "token=…", "contraseña: …" with a long,
# space-free value. The label is kept; only the value is replaced.
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_\- ]?key|secret(?:[_\- ]key)?|access[_\- ]token|auth[_\- ]token|token|password|passwd|"
    r"contrase(?:ñ|n)a|clave(?:[_\- ]api)?)(\s*[:=]\s*)([\"']?)([^\s\"'`]{12,})"
)


def redact_with_count(text: str | None) -> tuple[str, int]:
    """Return ``(redacted_text, number_of_redactions)``."""
    if not text:
        return text or "", 0
    n = 0

    def _pem(_: re.Match[str]) -> str:
        nonlocal n
        n += 1
        return "[REDACTED:private_key]"

    out = _PEM_BLOCK.sub(_pem, text)
    for kind, pat in _PATTERNS:
        out, k = pat.subn(f"[REDACTED:{kind}]", out)
        n += k

    def _assign(m: re.Match[str]) -> str:
        nonlocal n
        value = m.group(4)
        if value.startswith("[REDACTED:"):
            return m.group(0)
        n += 1
        return f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED:credential]"

    out = _ASSIGNMENT.sub(_assign, out)
    return out, n


def redact_secrets(text: str | None) -> str:
    """Redacted copy of ``text`` (empty string for ``None``)."""
    return redact_with_count(text)[0]
