"""Recorded provider responses ("cassettes") and the replaying provider.

A cassette is a JSON file next to the suite (``evals/<suite>/cassettes/<id>.json``)::

    {
      "cassette_version": 1,
      "case_id": "rag-pricing-cite",
      "origin": "synthetic",            # or "recorded" (maintainer, own key)
      "provider": "synthetic", "model": "synthetic",
      "request": {"messages": [{"role": "user", "content": "..."}]},
      "system_sha256": "...",           # informational: prompt drift warning only
      "response": {"text": "...", "usage": {"input_tokens": 0, "output_tokens": 42}}
    }

Replay is strict: a missing cassette, a request whose ``messages`` differ from
the recorded ones, or a second provider call all fail the case. There is no
fallback to a real provider and no network (see ``netguard``).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CASSETTE_VERSION = 1


class CassetteError(RuntimeError):
    code = "cassette_error"


class MissingCassette(CassetteError):
    code = "missing_cassette"


class CassetteMismatch(CassetteError):
    code = "cassette_mismatch"


class UnexpectedCall(CassetteError):
    code = "unexpected_provider_call"


def system_sha256(system: str) -> str:
    return hashlib.sha256(system.encode("utf-8")).hexdigest()


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MissingCassette(f"cassette not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CassetteError(f"cassette unreadable: {path}: {e}") from e
    if data.get("cassette_version") != CASSETTE_VERSION:
        raise CassetteError(f"unsupported cassette_version in {path}")
    if not isinstance(data.get("response", {}).get("text"), str):
        raise CassetteError(f"cassette has no response.text: {path}")
    return data


def _norm_messages(messages: list[dict]) -> list[dict]:
    return [{"role": str(m.get("role")), "content": str(m.get("content"))} for m in messages]


@dataclass
class CassetteProvider:
    """Duck-types ``brain.Provider``; replays exactly one recorded response."""

    path: Path
    name: str = "cassette"
    model: str = "cassette"
    calls: list[dict] = field(default_factory=list)
    error: CassetteError | None = None
    last_usage: dict | None = None
    stale_system: bool = False

    def available(self) -> bool:
        return True

    def supports_tools(self) -> bool:
        return False

    def ask(self, system: str, messages: list[dict], **kwargs: Any) -> str:
        self.calls.append({"system": system, "messages": _norm_messages(messages)})
        try:
            if len(self.calls) > 1:
                raise UnexpectedCall("provider called more than once; cassettes hold one turn")
            data = load(self.path)
            recorded = _norm_messages(data.get("request", {}).get("messages", []))
            if recorded != self.calls[-1]["messages"]:
                raise CassetteMismatch(
                    f"request messages differ from cassette {self.path.name}; re-record or fix the case")
            want = data.get("system_sha256")
            self.stale_system = bool(want) and want != system_sha256(system)
            self.model = str(data.get("model") or "cassette")
            self.last_usage = dict(data["response"].get("usage") or {})
            return str(data["response"]["text"])
        except CassetteError as e:
            self.error = e
            raise

    def stream(self, system: str, messages: list[dict], **kwargs: Any):  # type: ignore[no-untyped-def]
        yield self.ask(system, messages, **kwargs)

    def ask_with_tools(self, system: str, messages: list[dict], tools: list[dict],
                       on_tool_call: Any = None, **kwargs: Any) -> str:
        return self.ask(system, messages, **kwargs)
