"""Maintainer-only cassette recording (decision D9).

Re-records one case against a real provider **on the maintainer's own machine
with the maintainer's own key**:

    python -m ops_dashboard.evals record --suite brain-rag --case rag-pricing-cite \
        --provider anthropic --model claude-… --key-name anthropic_eval_key

Hard rules, each covered by a test:

* Refuses to run when any CI marker is set (``CI``, ``GITHUB_ACTIONS``, …) or
  ``CONDUCTLOOP_NO_LIVE_EVALS`` is set.
* Never reads keys from environment variables and never uses
  ``brain.resolve_provider`` (which does). The key comes only from the local
  secret store (macOS Keychain / 0600 file) under ``--key-name``.
* Writes to a directory outside the repo by default; a human reviews the file
  and copies it into ``evals/<suite>/cassettes/``. Response text is redacted;
  if the request itself contains a credential pattern nothing is written.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cassette as _cassette
from . import checks as _checks
from . import runner as _runner

CI_MARKERS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "CIRCLECI", "TF_BUILD",
              "JENKINS_URL", "TEAMCITY_VERSION", "CONDUCTLOOP_NO_LIVE_EVALS")
PROVIDERS = ("anthropic", "openai", "ollama")


class RecordRefused(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def ci_marker() -> str | None:
    for name in CI_MARKERS:
        v = os.environ.get(name)
        if v is not None and v.strip().lower() not in ("", "0", "false", "no"):
            return name
    return None


def refuse_in_ci() -> None:
    m = ci_marker()
    if m:
        raise RecordRefused("ci_detected", f"recording is maintainer-only and never runs in CI ({m} is set)")


def default_out_dir() -> Path:
    from .. import app_paths
    return app_paths.path("evals/recordings", "data/evals/recordings")


def _key_from_store(key_name: str) -> str:
    from .. import secrets_store
    v = secrets_store.get_secret(key_name)
    if not v:
        raise RecordRefused("key_not_found",
                            f"no key named {key_name!r} in the local secret store "
                            "(environment variables are never read)")
    return v


def build_provider(provider: str, model: str | None, key_name: str | None) -> Any:
    from .. import brain
    if provider not in PROVIDERS:
        raise RecordRefused("bad_provider", f"provider must be one of {PROVIDERS}")
    if provider == "ollama":
        return brain.OllamaProvider(model=model)
    if not key_name:
        raise RecordRefused("key_required", "--key-name is required for cloud providers")
    key = _key_from_store(key_name)
    if provider == "anthropic":
        return brain.AnthropicProvider(api_key=key, model=model)
    return brain.OpenAIProvider(api_key=key, model=model)


@dataclass
class RecordingProvider:
    inner: Any
    calls: list[dict] = field(default_factory=list)
    text: str | None = None
    last_usage: dict | None = None
    error: Any = None

    @property
    def name(self) -> str:
        return str(getattr(self.inner, "name", "provider"))

    @property
    def model(self) -> str:
        return str(getattr(self.inner, "model", "") or "")

    def available(self) -> bool:
        return True

    def supports_tools(self) -> bool:
        return False

    def ask(self, system: str, messages: list[dict], **kwargs: Any) -> str:
        self.calls.append({"system": system,
                           "messages": [{"role": m["role"], "content": m["content"]} for m in messages]})
        if len(self.calls) > 1:
            raise _cassette.UnexpectedCall("provider called more than once")
        self.text = str(self.inner.ask(system, messages))
        u = getattr(self.inner, "last_usage", None)
        self.last_usage = dict(u) if isinstance(u, dict) else None
        return self.text


def redact(text: str) -> str:
    out = text
    for kind, pat in _checks.SECRET_PATTERNS.items():
        out = pat.sub(f"[REDACTED:{kind}]", out)
    return out


def record_case(suite: _runner.Suite, case: _runner.Case, *, provider: str, model: str | None,
                key_name: str | None, out_dir: Path,
                provider_builder: Callable[[str, str | None, str | None], Any] = build_provider) -> Path:
    refuse_in_ci()
    if suite.target != "brain.ask":
        raise RecordRefused("no_cassette_target", f"suite {suite.name} does not use cassettes")
    rec = RecordingProvider(provider_builder(provider, model, key_name))
    with tempfile.TemporaryDirectory(prefix="cl-eval-rec-") as td:
        with _runner.case_env(suite, case, Path(td)):
            from .. import brain
            orig = brain.resolve_provider
            brain.resolve_provider = lambda *a, **k: rec  # type: ignore[assignment]
            try:
                brain.ask(case.prompt, k=int(case.params.get("k", 8)))
            finally:
                brain.resolve_provider = orig  # type: ignore[assignment]
    if not rec.calls or rec.text is None:
        raise RecordRefused("no_call", "the case did not call the provider")
    msgs = rec.calls[0]["messages"]
    if _checks.find_secrets(json.dumps(msgs)):
        raise RecordRefused("secret_in_request", "request contains a credential pattern; not writing")
    data = {
        "cassette_version": _cassette.CASSETTE_VERSION,
        "case_id": case.key,
        "origin": "recorded",
        "provider": rec.name,
        "model": rec.model,
        "request": {"messages": msgs},
        "system_sha256": _cassette.system_sha256(rec.calls[0]["system"]),
        "response": {"text": redact(rec.text), "usage": rec.last_usage or {}},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / suite.name / f"{case.key}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out
