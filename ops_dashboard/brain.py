"""ops-dashboard brain: BYOK RAG over the indexed knowledge.

This is the user's second brain. They ask in natural language, the brain retrieves
the most relevant documents (semantic + keyword), builds a context, and asks an
LLM to answer with citations.

Providers (BYOK):
  - anthropic   (Claude)        needs ANTHROPIC_API_KEY
  - openai      (GPT)           needs OPENAI_API_KEY
  - minimax     (MiniMax-M3)    needs MINIMAX_API_KEY (OpenAI-compatible)
  - ollama      (local)         needs Ollama running on localhost:11434
  - mock        (no key, no network)  useful for offline / testing

The provider is selected via the BRAIN_PROVIDER env var (default: 'auto' which
picks the first available). The model via BRAIN_MODEL.

Citations: we always return the top-K docs that were sent as context, so the
user can drill into any of them.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import app_paths as _app_paths
from . import byok_keys, db, embeddings, projects

log = logging.getLogger(__name__)

# Cap on a single synchronous LLM call made on an HTTP request thread. A local
# Ollama model can otherwise generate for 120s+ and hang the request. Bounded
# but generous — the user is actively waiting on brain chat. Env override:
# BRAIN_LLM_TIMEOUT (seconds).
LLM_TIMEOUT = float(os.environ.get("BRAIN_LLM_TIMEOUT", "90"))


def ask_with_deadline(provider, system: str, messages: list[dict],
                      seconds: float | None = None) -> str:
    """Run ``provider.ask()`` under a wall-clock deadline; raise TimeoutError if
    it exceeds it. The abandoned worker thread is not joined (executor shut down
    without waiting), so it ends when the underlying HTTP call returns."""
    import concurrent.futures as _cf
    seconds = LLM_TIMEOUT if seconds is None else seconds
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(provider.ask, system, messages)
    try:
        return fut.result(timeout=seconds)
    except _cf.TimeoutError:
        raise TimeoutError(f"{getattr(provider, 'name', 'llm')} exceeded {seconds:.0f}s deadline")
    finally:
        ex.shutdown(wait=False)


class ProviderError(RuntimeError):
    """A provider call failed with a user-actionable condition (auth, balance, rate, downstream).
    The .code attribute holds a stable identifier so the UI can pick an appropriate
    remedy; .message is safe-to-show for the user."""
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _translate_provider_error(e: Exception, provider_name: str) -> ProviderError:
    """Classify a provider exception and log its CODE in the opt-in local
    failure log (D6: off by default, codes only, no message text)."""
    err = _classify_provider_error(e, provider_name)
    from . import learning
    learning.record_failure("timeout" if isinstance(e, TimeoutError) else "provider_error",
                            err.code, provider=provider_name)
    return err


def _classify_provider_error(e: Exception, provider_name: str) -> ProviderError:
    """Map a raw openai/Anthropic SDK exception to a friendly ProviderError.

    Best-effort: if we can't classify it, we still wrap it as a generic error
    so the UI gets a clean message instead of an SDK traceback.
    """
    raw = str(e)
    # openai SDK uses status 401 / 402 / 429 / 5xx
    s = getattr(e, "status_code", None) or getattr(e, "status", None)
    # A bug on our side (bad SDK arguments...) never reached the provider:
    # don't blame it as "down", say what failed.
    if s is None and isinstance(e, (TypeError, AttributeError)):
        return ProviderError(
            "internal",
            f"Error interno de ConductLoop al llamar a '{provider_name}' (no llegó al provider): {type(e).__name__}: {raw[:160]}",
            500,
        )
    # also look inside the message
    for code in (402, 401, 429, 500, 502, 503, 504):
        if f"{code}" in raw or f"'http_code': '{code}'" in raw:
            s = s or code
            break
    s = s or 500
    if s == 404 or "not_found_error" in raw or "no longer available" in raw or "model_not_found" in raw:
        m = re.search(r"model[\s'\"]+([A-Za-z0-9._:/-]+)[\s'\"]+(?:is no longer|does not exist|not found)", raw)
        model = f" «{m.group(1)}»" if m else ""
        return ProviderError(
            "model_unavailable",
            f"El provider '{provider_name}' ya no ofrece el modelo{model}. Elige otro modelo o provider en Connect AI.",
            404,
        )
    if s == 402 or "insufficient_balance" in raw or "payment_required" in raw.lower():
        return ProviderError(
            "insufficient_credit",
            f"El provider '{provider_name}' ha rechazado la llamada: tu cuenta no tiene saldo. Recargar crédito o cambiar de provider en Settings.",
            402,
        )
    if s == 401 or "invalid_api_key" in raw or "authentication" in raw.lower():
        return ProviderError(
            "invalid_key",
            f"El provider '{provider_name}' ha rechazado la llamada: la API key es inválida o está revocada. Configurar una nueva en Settings.",
            401,
        )
    if s == 429 or "rate_limit" in raw.lower():
        return ProviderError(
            "rate_limited",
            f"El provider '{provider_name}' está limitado por tasa (rate limit). Espera unos segundos y reintenta.",
            429,
        )
    if s in (500, 502, 503, 504) or "internal server" in raw.lower():
        return ProviderError(
            "provider_down",
            f"El provider '{provider_name}' está caído o inestable ({s}). Reintenta en un rato o usa otro provider.",
            500,
        )
    # Connection-level errors
    name = type(e).__name__.lower()
    if "connection" in name or "timeout" in name or "connect" in raw.lower():
        return ProviderError(
            "unreachable",
            f"No se puede conectar con el provider '{provider_name}'. Comprueba la red o que el servicio esté accesible.",
            None,
        )
    # unknown
    log.warning("unclassified provider error (provider=%s): %s", provider_name, raw[:300])
    return ProviderError(
        "unknown",
        f"Error inesperado del provider '{provider_name}': {raw[:200]}",
        500,
    )

CONFIG_PATH = _app_paths.config_path()

# MiniMax's monthly Coding Plan is served on its Anthropic-compatible API, not
# on the pay-as-you-go balance (api.minimaxi.chat/v1).
MINIMAX_CODING_PLAN_URL = "https://api.minimax.io/anthropic"
# M3 reasons before answering, and the reasoning counts against max_tokens.
CODING_PLAN_MAX_TOKENS = 8000

DEFAULT_MODELS = {
    "anthropic": "claude-3-5-sonnet-latest",
    "openai":    "gpt-4o-mini",
    "ollama":    "llama3.2",
    # MiniMax retired MiniMax-M2.7 (404 "is no longer available", code 2069) while
    # its /models list still showed it.
    "minimax":   "MiniMax-M3",
    "gateway":   "openai/gpt-4o-mini",
    "mock":      "mock-1",
}

# Which models an OpenAI-compatible server has, asked once and remembered.
_OPENAI_MODELS_CACHE: dict[str, tuple[float, list[str] | None]] = {}
_OPENAI_MODELS_TTL = 60.0


# The order the brain tries providers in when nobody chose one. Local first.
_AUTO_ORDER = ("ollama", "anthropic", "openai", "gateway", "minimax", "mock")


def _auto_order(cfg: dict | None = None) -> list[str]:
    """The order the providers are tried in when nobody chose one.

    Local first: Ollama, then an OpenAI-compatible server on this machine,
    then the cloud providers, and the mock as a safe floor. The same rule
    the media providers follow — work done here is free and private.
    """
    cfg = cfg if cfg is not None else {}
    order = list(_AUTO_ORDER)
    openai_base = os.environ.get("OPENAI_BASE_URL") or cfg.get("openai_base_url")
    if _local_endpoint(openai_base):
        order.remove("openai")
        order.insert(1, "openai")
    return order


def _local_endpoint(base_url: str | None) -> bool:
    """Is this endpoint on this machine?

    Local work is free, private and has no rate limit, so a local server is
    used before a cloud one — the same rule the media providers follow.
    """
    if not base_url:
        return False
    host = base_url.split("//")[-1].split("/")[0].split(":")[0].strip().lower()
    if not host:
        return False
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local")

SYSTEM_PROMPT_BASE = """\
You are the user's ops brain. You have access to their personal knowledge base:
- Their projects and the work that rolls up under each.
- Their Obsidian-style vault (research, daily notes, projects, knowledge base).
- Live status of their monitored URLs.
- A structured playbook per project (Co-Pilots / Brains / Agents phases).
- A cross-source TODO/pendiente list.

You are direct, concise, and opinionated. Match the user's language. When you
cite a fact, append the source like [obsidian:filename.md] or [repo:name].
Don't make up information. If you don't know, say so.

Today's date: {today}

User preferences (from previous interactions):
{preferences}
"""

NO_CONTEXT_GUARD = (
    "\n[Guardrails for low-context answers — read carefully]\n"
    "- If you have NOT been given any document excerpts in this turn, AND the user's question\n"
    "  requires personal/specific knowledge they would only have in their vault\n"
    "  (e.g. 'what did I write about X', 'how is project Y doing'), respond EXACTLY:\n"
    "    'No tengo información sobre eso en tu vault.'\n"
    "  Do NOT answer from your own general knowledge.\n"
    "- If the provided excerpts exist but are clearly off-topic (score <0.2, no overlap\n"
    "  with the question), say so and ask the user for clarification.\n"
    "- NEVER fabricate a document, file path, URL, project name, or numerical fact.\n"
    "  If you would otherwise invent one, refuse instead.\n"
)


# ---------- providers ----------

class Provider:
    name: str = "base"
    # Token usage of the most recent ask(), as reported by the provider:
    # {"input_tokens": int, "output_tokens": int, "source": str} or None when
    # the provider didn't report it. Set by each ask(); callers that don't care
    # ignore it (return values are unchanged).
    last_usage: dict | None = None

    def available(self) -> bool:
        return True

    def ask(self, system: str, messages: list[dict], **kwargs) -> str:
        raise NotImplementedError

    def stream(self, system: str, messages: list[dict], **kwargs):
        """Yield text chunks. Default: just call ask() and yield the full text once."""
        yield self.ask(system, messages, **kwargs)

    def supports_tools(self) -> bool:
        return False

    def ask_with_tools(self, system: str, messages: list[dict], tools: list[dict], on_tool_call=None, **kwargs) -> str:
        """Provider-specific tool calling. Returns final text.
        on_tool_call(name, args, result) is called for each tool invocation.
        Default: not supported.
        """
        return self.ask(system, messages, **kwargs)


def _num(v) -> int | None:
    try:
        return None if v is None or isinstance(v, bool) else int(v)
    except (TypeError, ValueError):
        return None


def extract_usage(raw) -> dict | None:
    """Normalise a provider usage payload (SDK object or dict) to
    {"input_tokens", "output_tokens", "source"}; None if nothing usable.

    Anthropic: usage.input_tokens / output_tokens
    OpenAI, MiniMax, OpenAI-compatible gateways: usage.prompt_tokens /
      completion_tokens (MiniMax counts reasoning tokens inside completion)
    Ollama /api/chat: top-level prompt_eval_count / eval_count
    A payload with only total_tokens is reported as all-output with
    ``split_estimated`` (conservative: output is the pricier side)."""
    if raw is None:
        return None

    def get(k):
        return _num(raw.get(k) if isinstance(raw, dict) else getattr(raw, k, None))

    for a, b, src in (("input_tokens", "output_tokens", "input_output"),
                      ("prompt_tokens", "completion_tokens", "prompt_completion"),
                      ("prompt_eval_count", "eval_count", "ollama_eval_count")):
        i, o = get(a), get(b)
        if i is not None or o is not None:
            return {"input_tokens": i or 0, "output_tokens": o or 0, "source": src}
    t = get("total_tokens")
    if t is not None:
        return {"input_tokens": 0, "output_tokens": t, "source": "total_only",
                "split_estimated": True}
    return None


def _resp_usage(resp) -> dict | None:
    return extract_usage(resp.get("usage") if isinstance(resp, dict) else getattr(resp, "usage", None))


# Optional per-request gate. While a caller (the live evals' budget meter) has
# set it, every retry that ``_retry_transient`` would make is first shown to the
# gate — which may veto it by raising — and the SDK clients are built with
# ``max_retries=0`` so no hidden retry can bypass the caller's call/spend limits.
# Unset (the default) → behaviour is exactly as before.
_attempt_gate: contextvars.ContextVar = contextvars.ContextVar("brain_attempt_gate", default=None)
_retry_depth: contextvars.ContextVar = contextvars.ContextVar("brain_retry_depth", default=0)


def _sdk_retry_kwargs() -> dict:
    """``max_retries=0`` for the Anthropic/OpenAI SDK clients while a gate is set."""
    return {"max_retries": 0} if _attempt_gate.get() is not None else {}


def is_transient_error(e: BaseException) -> bool:
    """Connection-level / 5xx failures worth one retry (never 401/402/429)."""
    name = type(e).__name__.lower()
    raw = str(e).lower()
    return (
        "timeout" in name or "timeout" in raw
        or "connection" in name or "connect" in raw
        or "reset" in raw
        or "server_error" in name or "internal server" in raw
        or "502" in raw or "503" in raw or "504" in raw
    )


def _retry_transient(fn):
    """Decorator: retry once on connection-level transient failures (Fix #7).

    Network blips and 5xx upstream failures get one retry with a short backoff.
    4xx client errors (401/402/429) are NOT retried — they indicate config issues
    the user needs to fix, not transient noise.

    When ``_attempt_gate`` is set the retry is first offered to the gate (it can
    raise to stop it), and a decorated call nested inside another decorated call
    does not retry on its own: the outermost wrapper owns the single retry, so
    every network attempt is one gate decision.
    """
    import time as _t
    def wrapper(*args, **kwargs):
        gate = _attempt_gate.get()
        depth = _retry_depth.get()
        tok = _retry_depth.set(depth + 1)
        try:
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if not is_transient_error(e):
                    raise
                if gate is not None:
                    if depth > 0:
                        raise
                    gate(e)
                log.info("transient failure in %s — retrying once: %s", fn.__name__, str(e)[:120])
                _t.sleep(0.6)
                return fn(*args, **kwargs)
        finally:
            _retry_depth.reset(tok)
    return wrapper


class MockProvider(Provider):
    name = "mock"

    def __init__(self, model: str | None = None):
        # Optional model label so callers can pin distinct mock "models" per
        # session/test; None keeps the historic attribute-less behaviour.
        self.model = model

    def ask(self, system: str, messages: list[dict], **kwargs) -> str:
        last = messages[-1]["content"] if messages else ""
        # Without a key the brain can still show where the answer is: the
        # lines of the retrieved notes that share words with the question.
        found = _matching_lines(system, last)
        if found:
            return (
                "No AI key is set up, so ConductLoop cannot write an answer yet. "
                "These lines from your notes match your question:\n\n"
                + "\n\n".join(found)
                + f"\n\n{_NO_KEY_TIP}"
            )
        return (
            f"No AI key is set up, so ConductLoop cannot write an answer yet, "
            f"and nothing in your notes matches “{last[:200]}”.\n\n{_NO_KEY_TIP}"
        )


_NO_KEY_TIP = (
    "To get written answers, add a key in Credentials (Anthropic, OpenAI, "
    "MiniMax...) or point Connect AI at a local Ollama. From a terminal, "
    "BRAIN_PROVIDER=anthropic with ANTHROPIC_API_KEY works too."
)

# The context block written by _format_context: "[N] (source) title", then
# "path: ..." (or "content:" for web clips), then "content:" and the text.
_CONTEXT_START = re.compile(r"^Relevant context\b.*:$", re.M)
_CONTEXT_DOC = re.compile(r"^\[(\d+)\] \(([^)\n]*)\)(?: (.*))?$")
# What the prompt builders append after the last document.
_CONTEXT_END = ("[Guardrails for low-context answers", "[Untrusted web content", "<lessons>")
_STOPWORDS = frozenset(
    "the and for are was were what when where which who whom why how does did done "
    "have has had with from that this these those there their about into your you "
    "can could should would will shall may might must not but any all our out its "
    "que qué los las del una uno unos unas por para con cómo como cuándo cuando dónde "
    "donde cuál cual quién quien sobre tengo tiene hay mis tus sus esta este esto ese "
    "esa eso han fue ser está están".split()
)


def _terms(text: str) -> set[str]:
    words = re.findall(r"[^\W_]{3,}", text.lower())
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words if w not in _STOPWORDS}


def _matching_lines(system: str, question: str, *, docs: int = 3, per_doc: int = 2) -> list[str]:
    """For the no-key answer: up to ``per_doc`` lines from each of the first
    ``docs`` notes in the prompt's context block that share words with the
    question, as markdown ("**title** [N]" and quoted lines)."""
    starts = list(_CONTEXT_START.finditer(system or ""))
    terms = _terms(question)
    if not starts or not terms:
        return []
    blocks: list[dict] = []
    lines = system[starts[-1].end():].splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(_CONTEXT_END):
            break
        head = _CONTEXT_DOC.match(line)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if head and nxt.startswith(("path: ", "content:")):
            blocks.append({"n": head.group(1), "title": (head.group(3) or head.group(2)).strip(), "lines": []})
            i += 1
            continue
        if blocks:
            blocks[-1]["lines"].append(line)
        i += 1
    out: list[str] = []
    for b in blocks:
        body = b["lines"]
        # Drop the header lines and a YAML front matter at the top of the note.
        while body and (body[0].startswith(("path: ", "content:")) or not body[0].strip()):
            body = body[1:]
        if body and body[0].strip() == "---":
            end = next((k for k in range(1, len(body)) if body[k].strip() == "---"), None)
            body = body[end + 1:] if end is not None else body
        scored = []
        for k, raw in enumerate(body):
            text = re.sub(r"^#+\s*", "", raw.strip())
            if not text or text.startswith(("<<<", "url: ")):
                continue
            if text.startswith("title: "):
                b["title"] = text[7:].strip() or b["title"]
                continue
            if text.lower() == b["title"].lower():  # the note's own heading
                continue
            score = sum(1 for t in terms if t in text.lower())
            if score:
                scored.append((score, k, text if len(text) <= 240 else text[:239] + "…"))
        b["scored"] = scored
    # A line has to share at least half as many words with the question as the
    # best line does: one common word ("need") is not a match next to four.
    top = max((s[0] for b in blocks for s in b["scored"]), default=0)
    floor = max(1, (top + 1) // 2)
    for b in blocks:
        scored = [s for s in b["scored"] if s[0] >= floor]
        if not scored:
            continue
        best = sorted(sorted(scored, key=lambda s: (-s[0], s[1]))[:per_doc], key=lambda s: s[1])
        out.append(f"**{b['title']}** [{b['n']}]\n" + "\n".join(f"> {s[2]}" for s in best))
        if len(out) >= docs:
            break
    return out


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str | None = None, *, base_url: str | None = None):
        self.api_key = api_key
        self.model = model or DEFAULT_MODELS["anthropic"]
        # Anthropic-compatible endpoints (MiniMax's Coding Plan) take a base URL.
        self.base_url = base_url

    def available(self) -> bool:
        return bool(self.api_key)

    # No `temperature`: anthropic>=2 dropped the sampling parameters from
    # Messages.create/stream (current Claude models reject them), so passing
    # it raised TypeError before any request left the Mac.
    def _client(self):
        import anthropic
        if self.base_url:
            return anthropic.Anthropic(api_key=self.api_key, base_url=self.base_url, **_sdk_retry_kwargs())
        return anthropic.Anthropic(api_key=self.api_key, **_sdk_retry_kwargs())

    @_retry_transient
    def ask(self, system: str, messages: list[dict], **kwargs) -> str:
        client = self._client()
        out = []
        for m in messages:
            out.append({"role": m["role"], "content": m["content"]})
        self.last_usage = None
        msg = client.messages.create(
            model=self.model,
            system=system,
            messages=out,
            max_tokens=kwargs.get("max_tokens", 1500),
        )
        self.last_usage = _resp_usage(msg)
        parts = []
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "\n".join(parts).strip() or "(empty response)"

    def stream(self, system: str, messages: list[dict], **kwargs):
        client = self._client()
        out = []
        for m in messages:
            out.append({"role": m["role"], "content": m["content"]})
        with client.messages.stream(
            model=self.model,
            system=system,
            messages=out,
            max_tokens=kwargs.get("max_tokens", 1500),
        ) as stream:
            for text in stream.text_stream:
                if text:
                    yield text

    def supports_tools(self) -> bool:
        return True

    def ask_with_tools(self, system: str, messages: list[dict], tools: list[dict], on_tool_call=None, **kwargs) -> str:
        from . import tools as brain_tools
        client = self._client()
        out = []
        for m in messages:
            out.append({"role": m["role"], "content": m["content"]})
        # Loop until the model stops calling tools
        for _ in range(6):  # max iterations
            msg = client.messages.create(
                model=self.model,
                system=system,
                messages=out,
                tools=tools,
                max_tokens=kwargs.get("max_tokens", 1500),
                )
            # Collect tool_use blocks
            tool_blocks = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
            text_blocks = [b for b in msg.content if getattr(b, "type", None) == "text"]
            if not tool_blocks:
                return "\n".join(b.text for b in text_blocks).strip() or "(empty response)"
            # Append assistant message
            out.append({"role": "assistant", "content": [b.model_dump() for b in msg.content]})
            # Execute each tool and append tool_result
            tool_results = []
            for tb in tool_blocks:
                args = tb.input if isinstance(tb.input, dict) else {}
                result = brain_tools.call(tb.name, args)
                if on_tool_call:
                    try:
                        on_tool_call(tb.name, args, result)
                    except Exception:
                        pass
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tb.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
            out.append({"role": "user", "content": tool_results})
        return "(max tool iterations reached)"


class OpenAIProvider(Provider):
    """Any server that speaks the OpenAI API.

    OpenAI itself is the default, but the same code reaches DeepSeek, Groq,
    Together, LM Studio, llama.cpp, vLLM or Ollama's own ``/v1`` endpoint:
    they all speak this dialect, and the only things that change are the
    base url and the model name.

    A local server usually needs no key, so a base url on its own is enough
    to use it — and with no model configured the server is asked which
    models it has, so pointing this at a local runtime just works.
    """

    name = "openai"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None):
        self.api_key = api_key
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
        self.model = model or self._pick_model() or DEFAULT_MODELS["openai"]

    # ── what the server offers ────────────────────────────────────────────

    def _server_models(self) -> list[str] | None:
        """Which models this server has. Asked once and remembered.

        ``None`` means "could not ask", which is not the same as "has none":
        a server that is down must not look like a server with no models.
        """
        if not self.base_url:
            return None
        now = time.time()
        cached = _OPENAI_MODELS_CACHE.get(self.base_url)
        if cached and now - cached[0] < _OPENAI_MODELS_TTL:
            return cached[1]
        models: list[str] | None = None
        try:
            import httpx

            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            r = httpx.get(f"{self.base_url}/models", headers=headers, timeout=3.0)
            if r.status_code == 200:
                data = r.json()
                models = [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]
        except Exception:
            models = None
        _OPENAI_MODELS_CACHE[self.base_url] = (now, models)
        return models

    def _pick_model(self) -> str | None:
        models = self._server_models()
        return models[0] if models else None

    def available(self) -> bool:
        if self.base_url:
            # A local server needs no key; what it needs is to be there.
            return self._server_models() is not None
        return bool(self.api_key)

    def _client(self):
        try:
            import openai
        except ImportError:
            raise RuntimeError("the openai package is required for the OpenAI-compatible provider")
        # The SDK insists on a key; a local server ignores it.
        return openai.OpenAI(api_key=self.api_key or "local", base_url=self.base_url or None,
                             **_sdk_retry_kwargs())

    @_retry_transient
    def ask(self, system: str, messages: list[dict], **kwargs) -> str:
        client = self._client()
        msgs = [{"role": "system", "content": system}] + messages
        self.last_usage = None
        resp = client.chat.completions.create(
            model=self.model,
            messages=msgs,
            max_tokens=kwargs.get("max_tokens", 1500),
            temperature=kwargs.get("temperature", 0.3),
        )
        self.last_usage = _resp_usage(resp)
        return (resp.choices[0].message.content or "").strip() or "(empty response)"

    def stream(self, system: str, messages: list[dict], **kwargs):
        client = self._client()
        msgs = [{"role": "system", "content": system}] + messages
        with client.chat.completions.create(
            model=self.model,
            messages=msgs,
            max_tokens=kwargs.get("max_tokens", 1500),
            temperature=kwargs.get("temperature", 0.3),
            stream=True,
        ) as resp:
            for chunk in resp:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

    def supports_tools(self) -> bool:
        return True

    def ask_with_tools(self, system: str, messages: list[dict], tools: list[dict],
                       on_tool_call=None, **kwargs) -> str:
        """Agents need tool calling, so every OpenAI-compatible server gets it.

        The loop is the same one the other OpenAI-compatible providers use:
        the model asks for a tool, the tool runs, its answer goes back, and
        the model decides what to do next.
        """
        from . import tools as brain_tools

        client = self._client()
        msgs = [{"role": "system", "content": system}] + messages
        for _ in range(6):
            resp = client.chat.completions.create(
                model=self.model,
                messages=msgs,
                tools=tools,
                max_tokens=kwargs.get("max_tokens", 1500),
                temperature=kwargs.get("temperature", 0.3),
            )
            msg = resp.choices[0].message
            tool_calls = msg.tool_calls or []
            if not tool_calls:
                return (msg.content or "").strip() or "(empty response)"
            msgs.append(msg.model_dump())
            for tc in tool_calls:
                fn = tc.function
                tname = fn.name
                targs = fn.arguments
                if isinstance(targs, str):
                    try:
                        targs = json.loads(targs)
                    except Exception:
                        targs = {}
                result = brain_tools.call(tname, targs)
                if on_tool_call:
                    try:
                        on_tool_call(tname, targs, result)
                    except Exception:
                        log.debug("on_tool_call callback failed", exc_info=True)
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
        return "(max tool iterations reached)"


class MiniMaxProvider(Provider):
    """MiniMax provider.

    Defaults: model 'MiniMax-M3', base 'https://api.minimaxi.chat/v1' (OpenAI-
    compatible, pay-as-you-go balance). A base URL ending in '/anthropic' — e.g.
    MINIMAX_CODING_PLAN_URL, where the monthly Coding Plan subscription is
    served — switches every call to MiniMax's Anthropic-compatible API.
    """
    name = "minimax"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None):
        self.api_key = api_key
        self.model = model or DEFAULT_MODELS["minimax"]
        self.base_url = (base_url or os.environ.get("MINIMAX_BASE_URL")
                         or "https://api.minimaxi.chat/v1").rstrip("/")

    def available(self) -> bool:
        return bool(self.api_key)

    def _anthropic(self) -> AnthropicProvider | None:
        """The Anthropic-compatible client when the base URL points there."""
        if not self.base_url.endswith("/anthropic"):
            return None
        return AnthropicProvider(api_key=self.api_key, model=self.model, base_url=self.base_url)

    def _client(self):
        try:
            import openai
            return openai.OpenAI(api_key=self.api_key, base_url=self.base_url, **_sdk_retry_kwargs())
        except ImportError:
            raise RuntimeError("openai package required for MiniMax provider")

    @_retry_transient
    def ask(self, system: str, messages: list[dict], **kwargs) -> str:
        if (a := self._anthropic()) is not None:
            kwargs.setdefault("max_tokens", CODING_PLAN_MAX_TOKENS)
            out = a.ask(system, messages, **kwargs)
            self.last_usage = a.last_usage
            return out
        client = self._client()
        msgs = [{"role": "system", "content": system}] + messages
        self.last_usage = None
        resp = client.chat.completions.create(
            model=self.model,
            messages=msgs,
            max_tokens=kwargs.get("max_tokens", 1500),
            temperature=kwargs.get("temperature", 0.3),
        )
        self.last_usage = _resp_usage(resp)
        return (resp.choices[0].message.content or "").strip() or "(empty response)"

    def stream(self, system: str, messages: list[dict], **kwargs):
        if (a := self._anthropic()) is not None:
            kwargs.setdefault("max_tokens", CODING_PLAN_MAX_TOKENS)
            yield from a.stream(system, messages, **kwargs)
            return
        client = self._client()
        msgs = [{"role": "system", "content": system}] + messages
        with client.chat.completions.create(
            model=self.model,
            messages=msgs,
            max_tokens=kwargs.get("max_tokens", 1500),
            temperature=kwargs.get("temperature", 0.3),
            stream=True,
        ) as resp:
            for chunk in resp:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

    def supports_tools(self) -> bool:
        return True

    def ask_with_tools(self, system: str, messages: list[dict], tools: list[dict], on_tool_call=None, **kwargs) -> str:
        if (a := self._anthropic()) is not None:
            kwargs.setdefault("max_tokens", CODING_PLAN_MAX_TOKENS)
            return a.ask_with_tools(system, messages, tools, on_tool_call, **kwargs)
        from . import tools as brain_tools
        client = self._client()
        msgs = [{"role": "system", "content": system}] + messages
        for _ in range(6):
            resp = client.chat.completions.create(
                model=self.model,
                messages=msgs,
                tools=tools,
                max_tokens=kwargs.get("max_tokens", 1500),
                temperature=kwargs.get("temperature", 0.3),
            )
            msg = resp.choices[0].message
            tool_calls = msg.tool_calls or []
            if not tool_calls:
                return (msg.content or "").strip() or "(empty response)"
            msgs.append(msg.model_dump())
            for tc in tool_calls:
                fn = tc.function
                tname = fn.name
                targs = fn.arguments
                if isinstance(targs, str):
                    try: targs = json.loads(targs)
                    except Exception: targs = {}
                result = brain_tools.call(tname, targs)
                if on_tool_call:
                    try: on_tool_call(tname, targs, result)
                    except Exception: log.debug("on_tool_call callback failed", exc_info=True)
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
        return "(max tool iterations reached)"


class GatewayProvider(MiniMaxProvider):
    """Managed / multi-provider gateway (OpenAI-compatible): Vercel AI Gateway,
    OpenRouter, etc. One key, many models, observability. Token usage is metered
    into the usage log — the foundation for credits / subscription billing."""
    name = "gateway"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None):
        self.api_key = api_key
        self.model = model or DEFAULT_MODELS["gateway"]
        self.base_url = (base_url or os.environ.get("GATEWAY_BASE_URL")
                         or "https://ai-gateway.vercel.sh/v1").rstrip("/")

    def _meter(self, resp):
        try:
            u = getattr(resp, "usage", None)
            if u:
                from . import db
                db.log_usage(provider="gateway", model=self.model,
                             prompt_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
                             completion_tokens=int(getattr(u, "completion_tokens", 0) or 0))
        except Exception:
            pass

    @_retry_transient
    def ask(self, system: str, messages: list[dict], **kwargs) -> str:
        client = self._client()
        msgs = [{"role": "system", "content": system}] + messages
        self.last_usage = None
        resp = client.chat.completions.create(
            model=self.model, messages=msgs,
            max_tokens=kwargs.get("max_tokens", 1500),
            temperature=kwargs.get("temperature", 0.3),
        )
        self._meter(resp)
        self.last_usage = _resp_usage(resp)
        return (resp.choices[0].message.content or "").strip() or "(empty response)"


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, model: str | None = None, base_url: str = "http://localhost:11434"):
        # If user hasn't set a model, query /api/tags and pick the largest model
        # with chat capability (Fix #2 follow-up: e.g. a machine with qwen2.5:14b +
        # qwen2.5:7b + llama3.2; defaulting to 14b gives materially better answers
        # than the historic 2 GB llama3.2 default).
        self.base_url = base_url.rstrip("/")
        if model:
            self.model = model
        else:
            self.model = self._pick_best_local_model() or DEFAULT_MODELS["ollama"]

    def available(self) -> bool:
        try:
            import httpx
            r = httpx.get(f"{self.base_url}/api/tags", timeout=2.0)
            return r.status_code == 200
        except Exception:
            return False

    @staticmethod
    def _pick_best_local_model(base_url: str = "http://localhost:11434") -> str | None:
        try:
            import httpx
            r = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=2.0)
            r.raise_for_status()
            data = r.json()
            models = data.get("models", [])
            # chat-capable families; sorted desc by size
            chat_fams = {"llama", "qwen2", "qwen", "mistral", "mixtral", "command-r", "phi3", "gemma2"}
            cands = [
                m for m in models
                if (m.get("details", {}).get("family", "").lower() in chat_fams)
                and not m.get("name", "").endswith(":embed")
            ]
            if not cands:
                return None
            cands.sort(key=lambda m: m.get("size", 0), reverse=True)
            return cands[0]["name"]
        except Exception:
            return None

    @_retry_transient
    def ask(self, system: str, messages: list[dict], **kwargs) -> str:
        import httpx
        msgs = [{"role": "system", "content": system}] + messages
        payload = {"model": self.model, "messages": msgs, "stream": False}
        self.last_usage = None
        r = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=120.0)
        r.raise_for_status()
        data = r.json()
        self.last_usage = extract_usage(data) if isinstance(data, dict) else None
        return (data.get("message", {}).get("content") or "").strip() or "(empty response)"

    def stream(self, system: str, messages: list[dict], **kwargs):
        import httpx
        msgs = [{"role": "system", "content": system}] + messages
        payload = {"model": self.model, "messages": msgs, "stream": True}
        with httpx.stream("POST", f"{self.base_url}/api/chat", json=payload, timeout=120.0) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    import json as _json
                    chunk = _json.loads(line)
                except Exception:
                    continue
                delta = chunk.get("message", {}).get("content", "")
                if delta:
                    yield delta
                if chunk.get("done"):
                    break

    def supports_tools(self) -> bool:
        return True  # modern Ollama supports it

    def ask_with_tools(self, system: str, messages: list[dict], tools: list[dict], on_tool_call=None, **kwargs) -> str:
        import httpx

        from . import tools as brain_tools
        msgs = [{"role": "system", "content": system}] + messages
        for _ in range(6):
            payload = {"model": self.model, "messages": msgs, "tools": tools, "stream": False}
            r = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=120.0)
            r.raise_for_status()
            data = r.json()
            msg = data.get("message", {})
            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content", "")
            if not tool_calls:
                return (content or "").strip() or "(empty response)"
            # Append assistant turn
            msgs.append(msg)
            # Execute each tool
            for tc in tool_calls:
                fn = tc.get("function", {})
                tname = fn.get("name")
                targs = fn.get("arguments", {})
                if isinstance(targs, str):
                    try:
                        targs = json.loads(targs)
                    except Exception:
                        targs = {}
                result = brain_tools.call(tname, targs)
                if on_tool_call:
                    try:
                        on_tool_call(tname, targs, result)
                    except Exception:
                        pass
                msgs.append({
                    "role": "tool",
                    "content": json.dumps(result, ensure_ascii=False),
                })
        return "(max tool iterations reached)"


# ---------- provider resolution ----------

def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_config(cfg: dict) -> None:
    # Atomic replace + owner-only perms: config.json may still hold plaintext
    # BYOK keys when the OS keychain is unavailable (see byok_keys).
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, CONFIG_PATH)


# Config keys that hold a provider's API key (BYOK).
_PROVIDER_KEY_FIELD = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "minimax": "minimax_api_key",
    "gateway": "gateway_api_key",
}

# Fix #2 follow-up: remember providers that just blew up with auth/balance
# errors so we don't keep hammering them for the next 10 minutes.
_PROVIDER_LAST_FAILURE: dict[str, float] = {}
_PROVIDER_COOLDOWN_SECONDS = 600  # 10 min


def _provider_is_in_cooldown(name: str) -> bool:
    ts = _PROVIDER_LAST_FAILURE.get(name)
    if not ts:
        return False
    if (time.time() - ts) > _PROVIDER_COOLDOWN_SECONDS:
        _PROVIDER_LAST_FAILURE.pop(name, None)
        return False
    return True


def _mark_provider_failure(name: str) -> None:
    _PROVIDER_LAST_FAILURE[name] = time.time()
    log.warning("provider %s marked failed; cooldown %ss begins", name, _PROVIDER_COOLDOWN_SECONDS)


def _clear_provider_failure(name: str) -> None:
    if name in _PROVIDER_LAST_FAILURE:
        _PROVIDER_LAST_FAILURE.pop(name, None)


# Every time provider resolution switches away from what was configured or
# requested we record it here (and log a WARNING), so the switch is never
# silent. Bounded, in-memory, process-local; exposed via
# recent_provider_events() and GET /api/providers/events.
_PROVIDER_EVENTS: deque[dict] = deque(maxlen=100)
_PROVIDER_EVENTS_LOCK = threading.Lock()


def _record_provider_event(kind: str, **fields) -> dict:
    event = {"ts": int(time.time()), "kind": kind, **fields}
    with _PROVIDER_EVENTS_LOCK:
        _PROVIDER_EVENTS.append(event)
    log.warning("provider event %s: %s", kind,
                ", ".join(f"{k}={v}" for k, v in fields.items()))
    return event


def recent_provider_events(limit: int = 50) -> list[dict]:
    """Most recent provider fallback/refusal events, newest first."""
    with _PROVIDER_EVENTS_LOCK:
        items = list(_PROVIDER_EVENTS)
    return items[::-1][: max(0, limit)]


class ProviderUnavailableError(ProviderError):
    """An explicitly requested provider can't be used right now (e.g. cooldown).

    Raised instead of silently switching to another provider."""

    def __init__(self, provider: str, reason: str, message: str, *,
                 model: str | None = None, retry_after: int | None = None):
        super().__init__("provider_unavailable", message, status=503)
        self.provider = provider
        self.reason = reason
        self.model = model
        self.retry_after = retry_after

    def payload(self) -> dict:
        """Stable, secret-free JSON body for HTTP/SSE responses. The UI keys
        off ``error == "provider_unavailable"`` and shows ``provider``."""
        return {
            "ok": False,
            "error": "provider_unavailable",
            "code": "provider_unavailable",
            "provider": self.provider,
            "model": self.model,
            "retry_after": self.retry_after,
            "reason": self.reason,
            "message": self.message,
        }


def _provider_cooldown_remaining(name: str) -> int:
    """Seconds left in ``name``'s cooldown (0 if none)."""
    ts = _PROVIDER_LAST_FAILURE.get(name)
    if not ts:
        return 0
    return max(0, int(round(ts + _PROVIDER_COOLDOWN_SECONDS - time.time())))


def set_provider_key(provider: str, *, api_key: str | None = None,
                     model: str | None = None, base_url: str | None = None,
                     make_default: bool = False) -> dict:
    """BYOK: store a provider's API key / model / base_url locally.
    Returns the updated provider status list. Keys go to the OS keychain when
    available (config.json keeps only a ref) and otherwise to config.json on
    this machine, as before (see byok_keys). Never transmitted anywhere except
    to the provider."""
    provider = (provider or "").lower().strip()
    if provider not in ("anthropic", "openai", "minimax", "gateway", "ollama"):
        raise ValueError(f"unknown provider: {provider}")
    cfg = _load_config()
    if api_key is not None and provider in _PROVIDER_KEY_FIELD:
        byok_keys.store_api_key(provider, api_key, cfg)
    if model:
        cfg[f"{provider}_model"] = model.strip()
    if base_url:
        cfg[f"{provider}_base_url"] = base_url.strip()
    if make_default:
        cfg["default_provider"] = provider
    _save_config(cfg)
    return list_providers_status()


def list_providers_status() -> list[dict]:
    cfg = _load_config()
    out = []
    for name, cls in [
        ("anthropic", AnthropicProvider),
        ("openai",    OpenAIProvider),
        ("minimax",   MiniMaxProvider),
        ("gateway",   GatewayProvider),
        ("ollama",    OllamaProvider),
        ("mock",      MockProvider),
    ]:
        if name == "anthropic":
            p = AnthropicProvider(api_key=byok_keys.get_api_key("anthropic", cfg))
        elif name == "openai":
            p = OpenAIProvider(api_key=byok_keys.get_api_key("openai", cfg),
                               base_url=os.environ.get("OPENAI_BASE_URL") or cfg.get("openai_base_url"))
        elif name == "minimax":
            p = MiniMaxProvider(
                api_key=byok_keys.get_api_key("minimax", cfg),
                base_url=os.environ.get("MINIMAX_BASE_URL") or cfg.get("minimax_base_url"),
            )
        elif name == "gateway":
            p = GatewayProvider(
                api_key=byok_keys.get_api_key("gateway", cfg),
                model=cfg.get("gateway_model"),
                base_url=os.environ.get("GATEWAY_BASE_URL") or cfg.get("gateway_base_url"),
            )
        elif name == "ollama":
            p = OllamaProvider(model=os.environ.get("OLLAMA_MODEL") or cfg.get("ollama_model"))
        else:
            p = MockProvider()
        row = {"name": name, "available": p.available(), "model": getattr(p, "model", None),
               "base_url": getattr(p, "base_url", "") or "",
               "local": _local_endpoint(getattr(p, "base_url", "") or ""),
               "key_storage": byok_keys.key_storage(name, cfg) if name in byok_keys.PROVIDERS else None}
        if name == "minimax":
            row["plan"] = "coding" if isinstance(p, MiniMaxProvider) and p._anthropic() else "payg"
        out.append(row)
    return out


def resolve_provider(name: str | None = None, *, model: str | None = None,
                     strict: bool | None = None) -> Provider:
    """Pick a provider by name, or auto-pick the first available.

    ``model`` pins the model for this call only (it wins over the global
    ``BRAIN_MODEL`` / ``OLLAMA_MODEL`` env and config), so parallel callers can
    use different models of the same provider. It only applies to an explicit
    provider; with ``auto`` it is ignored (models are provider-specific).

    ``strict`` (default: True when ``name`` is passed explicitly and isn't
    ``"auto"``) refuses to switch providers: a requested provider in cooldown
    raises :class:`ProviderUnavailableError` instead of falling back. Non-strict
    resolution (no name → env/config default) keeps the historic fallback to
    ``auto`` but records it via :func:`recent_provider_events` + a WARNING log
    and tags the returned provider with ``fallback_from``.

    Auto priority (Fix #2): local Ollama first when up; then byok cloud providers
    in their declared order; finally mock as a guaranteed safe fallback.
    The order before this fix favored MiniMax just because it had a key configured,
    even when balance was 0 — so the user saw a 402 every time instead of a
    perfectly working local LLM sitting at :11434.
    """
    if strict is None:
        strict = name is not None and name != "" and name.lower() != "auto"
    cfg = _load_config()
    name = (name or os.environ.get("BRAIN_PROVIDER") or cfg.get("default_provider") or "auto").lower()
    fallback_from: str | None = None
    if name != "auto" and _provider_is_in_cooldown(name):
        if strict:
            _record_provider_event("refused", provider=name, model=model,
                                   reason="cooldown")
            raise ProviderUnavailableError(
                name, "cooldown",
                f"Provider '{name}' failed recently and is cooling down "
                f"(up to {_PROVIDER_COOLDOWN_SECONDS // 60} min). It was requested "
                f"explicitly, so ConductLoop won't switch to another provider. "
                f"Check its key/balance or pick another provider.",
                model=model, retry_after=_provider_cooldown_remaining(name),
            )
        # Non-strict (configured default): keep the historic behaviour — fall
        # through to a working provider — but never silently.
        fallback_from = name
        name = "auto"
    if name == "auto":
        if model:
            log.debug("resolve_provider: model %r ignored in auto mode", model)
        # Local first: Ollama, then an OpenAI-compatible server on this
        # machine, then the cloud providers, and the mock as a safe floor.
        order = _auto_order(cfg)
        status = list_providers_status()
        chosen: Provider = MockProvider()
        for n in order:
            if any(s["name"] == n and s["available"] for s in status) \
                    and not _provider_is_in_cooldown(n):
                chosen = resolve_provider(n, strict=False)
                break
        if fallback_from:
            _record_provider_event("fallback", requested=fallback_from,
                                   chosen=chosen.name, reason="cooldown")
            chosen.fallback_from = fallback_from  # type: ignore[attr-defined]
        return chosen
    if name == "anthropic":
        return AnthropicProvider(
            api_key=byok_keys.get_api_key("anthropic", cfg),
            model=model or os.environ.get("BRAIN_MODEL") or cfg.get("anthropic_model"),
        )
    if name == "openai":
        return OpenAIProvider(
            api_key=byok_keys.get_api_key("openai", cfg),
            model=model or os.environ.get("BRAIN_MODEL") or cfg.get("openai_model"),
            base_url=os.environ.get("OPENAI_BASE_URL") or cfg.get("openai_base_url"),
        )
    if name == "minimax":
        return MiniMaxProvider(
            api_key=byok_keys.get_api_key("minimax", cfg),
            model=model or os.environ.get("BRAIN_MODEL") or cfg.get("minimax_model"),
            base_url=os.environ.get("MINIMAX_BASE_URL") or cfg.get("minimax_base_url"),
        )
    if name == "gateway":
        return GatewayProvider(
            api_key=byok_keys.get_api_key("gateway", cfg),
            model=model or os.environ.get("BRAIN_MODEL") or cfg.get("gateway_model"),
            base_url=os.environ.get("GATEWAY_BASE_URL") or cfg.get("gateway_base_url"),
        )
    if name == "ollama":
        return OllamaProvider(
            model=model or os.environ.get("OLLAMA_MODEL") or cfg.get("ollama_model"),
            base_url=os.environ.get("OLLAMA_BASE_URL") or cfg.get("ollama_base_url", "http://localhost:11434"),
        )
    if name == "mock":
        return MockProvider(model=model)
    raise ValueError(f"unknown provider: {name}")


def set_config(key: str, value: str) -> None:
    """Store a config value in config.json (see app_paths.config_path)."""
    cfg = _load_config()
    cfg[key] = value
    _save_config(cfg)


# ---------- retrieval ----------

def _build_project_keywords() -> re.Pattern:
    """Intent regex derived from the project registry (identity-neutral).

    Empty registry → a pattern that never matches, so project detection is
    simply off until the user defines their own projects.
    """
    try:
        from . import projects as _proj
        terms: set[str] = set()
        for p in _proj.PROJECTS:
            terms.add(re.escape(p.key))
            for a in p.aliases:
                if a.strip():
                    terms.add(re.escape(a.strip()))
        if terms:
            return re.compile(r"\b(" + "|".join(sorted(terms)) + r")\b", re.IGNORECASE)
    except Exception:
        pass
    return re.compile(r"(?!x)x")  # matches nothing


PROJECT_KEYWORDS = _build_project_keywords()

REVENUE_KEYWORDS = re.compile(
    r"\b("
    r"revenue|ingresos?|ganancias?|facturaci[oó]n|ventas?|subscriptions?|suscriptores?|"
    r"mrr|arr|churn|"
    r"cu[aá]nto\s+(he\s+)?(ganado|vendido|facturado|ingresado)|"
    r"money|paid|payments?|charges?"
    r")\b",
    re.IGNORECASE,
)

GA_KEYWORDS = re.compile(
    r"\b("
    r"analytics|tr[aá]fico|visitas?|usuarios?|visitantes?|page[ -]?views?|sessions?|"
    r"tr[aá]fico\s+(de|web|del\s+sitio)|"
    r"cu[aá]nta\s+(gente|visitas|usuarios|visitantes|tr[aá]fico)\s+(ha\s+)?(venido|entrado|visitado)?|"
    r"cu[aá]nto\s+tr[aá]fico|"
    r"bounce|engagement|"
    r"qu[eé]\s+tanto\s+(recibe|entra|llega)\s+(a|en)\s+(la\s+)?(web|p[aá]gina|sitio)"
    r")\b",
    re.IGNORECASE,
)

SENTRY_KEYWORDS = re.compile(
    r"\b("
    r"sentry|errores?|bugs?|excepciones?|crashes?|stack[ -]?traces?|"
    r"qu[eé]\s+(errores?|bugs?|excepciones?|crashes)\s+(hay|tengo|tiene)?|"
    r"issues?\s+(de|en)\s+(sentry|producci[oó]n|proyecto)|"
    r"algo\s+(roto|ca[ií]do|fallando)|"
    r"se\s+(cay[oó]|rompi[oó]|ha\s+ca[ií]do)|"
    r"est[aá]\s+(roto|ca[ií]do|fallando)"
    r")\b",
    re.IGNORECASE,
)

ANOMALY_KEYWORDS = re.compile(
    r"\b("
    r"anomal[ií]as?|alertas?|spikes?|drops?|ca[ií]das?\s+repentinas?|"
    r"qu[eé]\s+(anomal[ií]as?|alertas)\s+hay|"
    r"hay\s+algo\s+(raro|extra[nñ]o|fuera\s+de\s+lo\s+normal)|"
    r"detect[ao]r\s+problemas"
    r")\b",
    re.IGNORECASE,
)


def _detect_project(question: str) -> str | None:
    m = PROJECT_KEYWORDS.search(question)
    if not m:
        return None
    proj = projects.find_project(m.group(1))
    return proj.key if proj else None


def _build_revenue_context(question: str, project_key: str | None = None) -> str:
    """When the question is about money/revenue, build a tight summary block.

    Includes: total, by source, by project, top 5 events.
    If a project_key is detected, focuses on that project.
    """
    try:
        summary = db.revenue_summary(project_key=project_key)
        if summary.get("count", 0) == 0:
            return ""
        # If a month is mentioned (e.g. "mayo", "june", "2026-05"), filter by since
        month_match = re.search(r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre|\d{4}-\d{2})\b", question, re.IGNORECASE)
        since = None
        if month_match:
            m = month_match.group(1).lower()
            months = {"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,"julio":7,"agosto":8,"septiembre":9,"octubre":10,"noviembre":11,"diciembre":12}
            if m in months:
                # Last occurrence of that month (most recent)
                now = time.gmtime()
                yr = now.tm_year
                if months[m] > now.tm_mon:
                    yr -= 1
                import calendar
                _, last_day = calendar.monthrange(yr, months[m])
                since = int(time.mktime((yr, months[m], 1, 0, 0, 0, 0, 0, 0)))
                until = int(time.mktime((yr, months[m], last_day, 23, 59, 59, 0, 0, 0))) + 1
                summary = db.revenue_summary(project_key=project_key, since=since, until=until)
        lines = []
        title = "Revenue"
        if since:
            import calendar as _cal
            yr = time.gmtime(since).tm_year
            mo = time.gmtime(since).tm_mon
            title = f"Revenue {yr}-{mo:02d} ({_cal.month_name[mo]} {yr})"
        if project_key:
            title += f" for project '{project_key}'"
        lines.append(f"{title}: €{summary['total_cents']/100:,.2f} from {summary['count']} events")
        if summary.get("by_source"):
            lines.append("by source: " + ", ".join(
                f"{r['source_type']}=€{r['total']/100:,.0f}({r['cnt']})"
                for r in summary["by_source"][:5]
            ))
        if summary.get("by_project"):
            lines.append("by project: " + ", ".join(
                f"{r['project_key']}=€{r['total']/100:,.0f}({r['cnt']})"
                for r in summary["by_project"][:6]
            ))
        if summary.get("by_type"):
            lines.append("by event type: " + ", ".join(
                f"{r['event_type']}=€{r['total']/100:,.0f}({r['cnt']})"
                for r in summary["by_type"][:5]
            ))
        # Top 5 most recent events
        events = db.list_revenue_events(project_key=project_key, limit=5)
        if events:
            lines.append("recent events:")
            for e in events:
                d = time.strftime("%Y-%m-%d", time.gmtime(e["occurred_at"]))
                lines.append(f"  {d} {e['source_type']} {e['project_key']} {e['event_type']} €{e['amount_cents']/100:+.2f} ({e['status']})")
        return "\n".join(lines)
    except Exception as e:
        return f"(revenue lookup failed: {e})"


def _build_ga_context(question: str, project_key: str | None = None) -> str:
    """When the question is about traffic/analytics, build a tight summary.

    Includes: total users/sessions/page_views, per-site breakdown, recent daily trend.
    """
    try:
        # Determine period: default last 7 days, look for "mes" or "30d" etc.
        days = 7
        m = re.search(r"\b(\d{1,3})\s*(d[ií]as?|days?)\b", question, re.IGNORECASE)
        if m:
            days = max(1, min(int(m.group(1)), 365))
        elif re.search(r"\b(mes|month|este\s+mes|last\s+month)\b", question, re.IGNORECASE):
            days = 30
        # Compute since date
        since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        summary = db.ga_summary(project_key=project_key, since=since)
        totals = summary.get("totals", {})
        if totals.get("users", 0) == 0:
            return ""
        lines = []
        title = f"GA4 traffic (last {days}d"
        if project_key:
            title += f" for project '{project_key}'"
        title += ")"
        lines.append(f"{title}: {totals.get('users', 0):,} users, {totals.get('sessions', 0):,} sessions, {totals.get('page_views', 0):,} page views")
        eng = totals.get("avg_engagement_rate", 0) * 100
        bounce = totals.get("avg_bounce_rate", 0) * 100
        lines.append(f"engagement {eng:.0f}%, bounce {bounce:.0f}%, avg session {totals.get('avg_session_duration', 0):.0f}s")
        if summary.get("by_site"):
            lines.append("by site: " + ", ".join(
                f"{r['display_name']}={r['users']:,}u/{r['sessions']:,}s"
                for r in summary["by_site"][:8]
            ))
        # Recent daily trend
        daily = db.ga_daily_totals(project_key=project_key, since=since)
        if daily:
            recent = daily[-7:]
            lines.append("recent days: " + ", ".join(
                f"{d['date'][-5:]}={d['users']:,}u" for d in recent
            ))
        return "\n".join(lines)
    except Exception as e:
        return f"(ga lookup failed: {e})"


def _build_sentry_context(question: str, project_key: str | None = None) -> str:
    """When the question is about errors/bugs/Sentry, build a tight summary.

    Includes: total issues by level, top 10 most-frequent unresolved issues,
    recent activity. Filters by project if detected.
    """
    try:
        days = 7
        m = re.search(r"\b(\d{1,3})\s*(d[ií]as?|days?)\b", question, re.IGNORECASE)
        if m: days = max(1, min(int(m.group(1)), 90))
        elif re.search(r"\b(mes|month|esta\s+semana)\b", question, re.IGNORECASE): days = 30
        since = int(time.time()) - days * 86400
        summary = db.sentry_summary(since=since)
        if summary["total"] == 0:
            return ""
        lines = []
        title = f"Sentry issues (last {days}d"
        if project_key: title += f" for '{project_key}'"
        title += ")"
        lines.append(f"{title}: {summary['total']} total")
        if summary.get("by_level"):
            lines.append("by level: " + ", ".join(
                f"{r['level']}={r['cnt']}" for r in summary["by_level"]
            ))
        # Top 10 most-frequent unresolved
        issues = db.list_sentry_issues(project_key=project_key, status="unresolved", limit=10)
        if issues:
            lines.append("top unresolved issues:")
            for it in issues:
                ts = time.strftime("%Y-%m-%d", time.gmtime(it["last_seen"]))
                lines.append(f"  {ts} [{it['level']}] {it['title'][:60]} ({it['event_count']} events, last in {it['culprit'][:40] if it['culprit'] else 'unknown'})")
        return "\n".join(lines)
    except Exception as e:
        return f"(sentry lookup failed: {e})"


def _build_anomaly_context(question: str, project_key: str | None = None) -> str:
    """When the question is about anomalies/alerts, build a tight summary."""
    try:
        from .indexers import anomalies as _anomalies
        result = _anomalies.detect_recent_anomalies()
        if not result["anomalies"]:
            return ""
        lines = []
        lines.append(f"Detected {result['count']} anomalies (high: {result['by_severity']['high']}, medium: {result['by_severity']['medium']}):")
        for a in result["anomalies"][:8]:
            sev = a["severity"]
            icon = "🔴" if sev == "high" else ("🟡" if sev == "medium" else "🔵")
            lines.append(f"  {icon} {a.get('message')}")
        return "\n".join(lines)
    except Exception as e:
        return f"(anomaly lookup failed: {e})"


# Words that carry no retrieval signal in the (mostly ES/EN) questions users ask.
_KEYWORD_STOPWORDS = frozenset("""
a al algo algún alguna como con cual cuál cuando cuándo de del desde donde dónde el ella ellos en entre
era es esa ese eso esta este esto estos fue han hay la las le les lo los me mi mis muy más nos o para
pero por porque que qué quien quién se sea ser si sin sobre son su sus también te tengo tiene tu tus
un una uno unos usa y ya yo decidimos escribí hicimos dime cuenta explica resume
about after all also and any are can did does for from had has have how into its just more not now
our out than that the their them then there these they this was were what when where which who why
will with would you your tell show give
""".split())


def _keyword_terms(question: str, max_terms: int = 6) -> list[str]:
    """Distinct content words of the question, longest first (most specific)."""
    seen: list[str] = []
    for tok in re.findall(r"\w+", question.casefold()):
        if len(tok) < 3 or tok.isdigit() or tok in _KEYWORD_STOPWORDS or tok in seen:
            continue
        seen.append(tok)
    return sorted(seen, key=len, reverse=True)[:max_terms]


def _keyword_retrieve(question: str, *, limit: int = 16) -> list[dict]:
    """Keyword fallback for _retrieve when semantic search returns nothing.

    Runs db.search per content word and ranks documents by the fraction of
    the question's words they contain, so the score is comparable to the
    0..1 cosine scale (and the low-context guard still fires on weak hits).
    """
    terms = _keyword_terms(question)
    if not terms:
        return []
    hits: dict[int, dict] = {}
    for term in terms:
        try:
            found = db.search(term, limit=limit * 2)
        except Exception:
            log.debug("keyword fallback search failed for %r", term, exc_info=True)
            continue
        for r in found:
            h = hits.setdefault(r["id"], {"doc_id": r["id"], "source": r["source"], "path": r["path"],
                                          "title": r["title"], "matched": 0, "in_title": 0, "excerpt": ""})
            h["matched"] += 1
            if term in (r["title"] or "").casefold():
                h["in_title"] += 1
    # More distinct words first; ties go to docs whose title holds the words.
    ranked = sorted(hits.values(), key=lambda h: (-h["matched"], -h["in_title"], h["path"]))
    out = []
    for h in ranked[:limit]:
        h.pop("in_title")
        h["score"] = round(h.pop("matched") / len(terms), 3)
        out.append(h)
    return out


def _filter_by_project(rows: list[dict], project_key: str | None) -> list[dict]:
    """Keep only the documents that belong to this project (plus shared ones).

    The RAG used to search every document of every project, so a question
    about one business answered with another business's files — and cited
    them. Now a question about a project only sees that project (and the
    documents no project owns). 2026-10-23.
    """
    if not project_key or not rows:
        return rows
    ids = [r["doc_id"] for r in rows if r.get("doc_id") is not None]
    if not ids:
        return rows
    owner: dict[int, str | None] = {}
    try:
        with db.db() as conn:
            marks = ",".join("?" * len(ids))
            for r in conn.execute(
                f"SELECT id, project_key FROM documents WHERE id IN ({marks})", ids  # noqa: S608 - fixed placeholders
            ):
                owner[r["id"]] = r["project_key"]
    except Exception:
        # No project_key column yet (older DB): keep the old behaviour.
        return rows
    if not owner:
        return rows
    keep: set[int] = set()
    for r in rows:
        doc_id = r.get("doc_id")
        if doc_id not in owner:
            continue  # unknown document: leave it out, it is not ours
        pkey = owner[doc_id]
        # A note that belongs to nobody is an idea, and an idea is welcome
        # everywhere. A note that belongs to another project stays out:
        # the notes that talk about this project are attributed to it by
        # notes_structure, so nothing is lost by keeping the line here.
        if pkey == project_key or pkey is None:
            keep.add(doc_id)
    if not keep:
        # Nothing of this project (and nothing shared): an honest empty
        # answer beats another business's files.
        return []
    return [r for r in rows if r.get("doc_id") in keep]


def _retrieve(question: str, *, k: int = 8, project_key: str | None = None) -> list[dict]:
    """Retrieve top-K most relevant documents.

    Strategy:
      1. Semantic search via embeddings (cosine).
      1b. If that yields nothing (lean desktop build without the embedding
          model, or nothing embedded yet), fall back to keyword search.
      2. If a project_key is detected/forced, keep only that project's
         documents (and the shared ones), then boost the ones that mention
         the project.
      3. Return top-K with metadata for citations.
    """
    # Try semantic
    try:
        rows = embeddings.search(question, limit=k * 2)
    except Exception:
        rows = []
    if not rows:
        rows = _keyword_retrieve(question, limit=k * 2)
    # Isolate by project: never answer about business A with business B's
    # files (2026-10-23). And the detected project's own documents go first:
    # appended after the similarity hits they never made the top K, and
    # similarity alone ranks look-alike words above them ("ConductLoop" →
    # "Code of Conduct").
    rows = _filter_by_project(rows, project_key)
    if project_key:
        needle = f"%{project_key.lower()}%"
        with db.db() as conn:
            proj_docs = conn.execute(
                # The column that says who owns a document is what decides.
                # The path and title are a second way in, but only for a
                # document that no project owns yet: matching the path of
                # another project's file would put it back in the answer.
                "SELECT id, source, path, title, mtime FROM documents "
                "WHERE project_key = ? OR (project_key IS NULL AND "
                "(lower(path) LIKE ? OR lower(coalesce(title, '')) LIKE ?)) "
                "ORDER BY mtime DESC LIMIT 200",
                (project_key, needle, needle),
            ).fetchall()
        ids = {d["id"] for d in proj_docs}
        rows = [{"doc_id": d["id"], "source": d["source"], "path": d["path"], "title": d["title"],
                 "score": 1.0, "excerpt": ""} for d in proj_docs] + [r for r in rows if r.get("doc_id") not in ids]
    # Fetch full content for the top K
    out: list[dict] = []
    for r in rows[:k]:
        doc_id = r.get("doc_id")
        if doc_id is None:
            continue
        with db.db() as conn:
            drow = conn.execute("SELECT content FROM documents WHERE id = ?", (doc_id,)).fetchone()
        text = (drow["content"] if drow else "")[:2000]
        out.append({
            "doc_id": r["doc_id"],
            "source": r["source"],
            "path": r["path"],
            "title": r["title"] or Path(r["path"]).name,
            "score": r.get("score", 0.0),
            "excerpt": text[:400],
            "content": text,
        })
    return out


def _format_context(docs: list[dict]) -> tuple[str, list[dict]]:
    """Build a context block + a list of citations.

    Vault content is redacted (API keys, tokens, private keys…) before it goes
    into the prompt: the prompt leaves the Mac, the credential must not."""
    from .redact import redact_secrets
    parts = []
    citations = []
    for i, d in enumerate(docs, 1):
        cite = {
            "n": i,
            "source": d["source"],
            "path": d["path"],
            "title": d["title"],
            "score": d.get("score", 0.0),
        }
        citations.append(cite)
        if d.get("untrusted"):
            # Third-party text (web clips): title and path are page-controlled,
            # so they travel inside the already-wrapped content, never before it.
            parts.append(f"[{i}] ({d['source']})\ncontent:\n{redact_secrets(d['content'])}\n")
            continue
        # Titles and paths come from filenames and H1s, which can hold a
        # credential just like the body.
        parts.append(
            f"[{i}] ({d['source']}) {redact_secrets(str(d['title']))}\n"
            f"path: {redact_secrets(str(d['path']))}\n"
            f"content:\n{redact_secrets(d['content'])}\n"
        )
    return "\n".join(parts), citations


# ---------- ask ----------

@dataclass
class BrainAnswer:
    answer: str
    citations: list[dict]
    provider: str
    model: str | None
    project_key: str | None = None
    duration_ms: int = 0
    memory_proposals: list[str] = field(default_factory=list)
    lessons_used: list[int] = field(default_factory=list)


def apply_lessons(system: str, question: str, project_key: str | None = None) -> tuple[str, list[int]]:
    """L4 hook for prompt builders outside ask() (e.g. /api/brain/stream). Never raises."""
    try:
        from .learning import lessons as _lessons
        return _lessons.apply_to_system(system, question, project_key=project_key)
    except Exception:
        log.debug("lessons injection failed", exc_info=True)
        return system, []


def mark_lessons_used(ids: list[int], answer_text: str) -> None:
    try:
        from .learning import lessons as _lessons
        _lessons.mark_used(ids, answer_text)
    except Exception:
        log.debug("lessons mark_used failed", exc_info=True)


def _log_broken_citations(answer_text: str, citations: list[dict], provider: Provider) -> None:
    """Opt-in failure log: the answer cites [N] beyond the sources it was given."""
    refs = {int(n) for n in re.findall(r"\[(\d{1,3})\]", answer_text or "")}
    if any(r < 1 or r > len(citations) for r in refs):
        from . import learning
        learning.record_failure("broken_citation", "citation_out_of_range",
                                provider=provider.name, model=getattr(provider, "model", None))


def ask(question: str, *, conversation_id: int | None = None,
        provider_name: str | None = None, k: int = 8, use_tools: bool = False,
        project_key: str | None = None) -> BrainAnswer:
    """Ask the brain a question. Optionally continue a conversation.

    If use_tools is True and the provider supports function calling, the brain
    can invoke tools to fetch live data (URLs, TODOs, project status, etc.).

    ``project_key`` scopes the question to one business: it overrides the
    detection from the question text, and the RAG then only cites that
    project's documents (2026-10-23).
    """
    t0 = time.perf_counter()
    project_key = project_key or _detect_project(question)
    docs = _retrieve(question, k=k, project_key=project_key)
    # Web clips (browser PR 6) are third-party text: wrap them as untrusted,
    # and keep them out of tool-using turns entirely.
    from .web_ask import guard_web_docs
    docs, web_rules = guard_web_docs(docs, drop=use_tools)
    context, citations = _format_context(docs)

    # If the question is about revenue/money, prepend a tight revenue summary
    revenue_block = ""
    if REVENUE_KEYWORDS.search(question):
        revenue_block = _build_revenue_context(question, project_key=project_key)
        if revenue_block:
            context = revenue_block + ("\n\n" + context if context else "")

    # If the question is about traffic/analytics, prepend a tight GA summary
    ga_block = ""
    if GA_KEYWORDS.search(question):
        ga_block = _build_ga_context(question, project_key=project_key)
        if ga_block:
            context = ga_block + ("\n\n" + context if context else "")

    # If the question is about Sentry/errors, prepend a tight issues summary
    sentry_block = ""
    if SENTRY_KEYWORDS.search(question):
        sentry_block = _build_sentry_context(question, project_key=project_key)
        if sentry_block:
            context = sentry_block + ("\n\n" + context if context else "")

    # If the question is about anomalies/alerts, prepend a tight anomaly summary
    anomaly_block = ""
    if ANOMALY_KEYWORDS.search(question):
        anomaly_block = _build_anomaly_context(question, project_key=project_key)
        if anomaly_block:
            context = anomaly_block + ("\n\n" + context if context else "")

    today = time.strftime("%Y-%m-%d")
    # Load persistent preferences
    prefs = db.memory_list()
    pref_str = "\n".join(f"- {p['key']}: {p['value']}" for p in prefs) or "(none yet)"
    system = SYSTEM_PROMPT_BASE.format(today=today, preferences=pref_str) + (
        "\n\nRelevant context (cite with [N]):\n" + (context if context else "(no matches found)")
    )
    # L4: approved lessons for this scope (<= 5 and ~800 tokens, D12).
    system, lessons_used = apply_lessons(system, question, project_key)

    # Build messages: history + new question
    messages: list[dict] = []
    if conversation_id:
        with db.db() as conn:
            prior = conn.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at",
                (conversation_id,),
            ).fetchall()
        for m in prior:
            messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": question})

    provider = resolve_provider(provider_name)
    # Inject retrieval-strength signals into the system prompt so the LLM
    # can self-assess whether it has enough context to answer (Fix #3).
    retrieval_note = ""
    if not docs or all(d.get("score", 0.0) < 0.2 for d in docs):
        retrieval_note = NO_CONTEXT_GUARD
    system += retrieval_note
    system += web_rules

    if use_tools and provider.supports_tools():
        from . import tools as brain_tools
        from .skills import catalog as skills_catalog
        prov_tools = brain_tools.tools_for_anthropic() if provider.name == "anthropic" else brain_tools.tools_for_openai()
        # L5: catalog of enabled skills (name + description); empty when none.
        system += skills_catalog.catalog_prompt()
        # Capture on_tool_call for system-prompt-level tool log
        tool_log: list[dict] = []
        def _on_call(name, args, result):
            tool_log.append({"name": name, "args": args, "ok": result.get("ok", False)})
        try:
            with skills_catalog.activation_scope():
                answer_text = provider.ask_with_tools(system, messages, prov_tools, on_tool_call=_on_call)
            _clear_provider_failure(provider.name)
        except Exception as e:
            err = _translate_provider_error(e, provider.name)
            if err.code in ("insufficient_credit", "invalid_key", "provider_down"):
                _mark_provider_failure(provider.name)
            raise err from e
    else:
        try:
            answer_text = ask_with_deadline(provider, system, messages)
            _clear_provider_failure(provider.name)
        except Exception as e:
            err = _translate_provider_error(e, provider.name)
            if err.code in ("insufficient_credit", "invalid_key", "provider_down"):
                _mark_provider_failure(provider.name)
            raise err from e
        tool_log = []
    duration_ms = int((time.perf_counter() - t0) * 1000)
    _log_broken_citations(answer_text, citations, provider)
    mark_lessons_used(lessons_used, answer_text)

    # Auto-memory: propose durable facts from the exchange so the brain can
    # learn. Opt-in save via BRAIN_AUTO_MEMORY=save; otherwise just surfaced.
    proposals: list[str] = []
    try:
        from . import memory_ai
        # The mock provider's placeholder text is not a fact about the user.
        source = question if provider.name == "mock" else f"{question}\n{answer_text}"
        proposals = memory_ai.propose_from_text(source, max_items=3)
        # Never auto-save memory from a turn that saw web content.
        if proposals and os.environ.get("BRAIN_AUTO_MEMORY") == "save" and not web_rules:
            memory_ai.remember_proposals(proposals)
    except Exception:
        log.debug("auto-memory proposal failed", exc_info=True)

    return BrainAnswer(
        answer=answer_text,
        citations=citations,
        provider=provider.name,
        model=getattr(provider, "model", None),
        project_key=project_key,
        duration_ms=duration_ms,
        memory_proposals=proposals,
        lessons_used=lessons_used,
    )


# ---------- morning briefing ----------

def briefing() -> str:
    """Compose a structured morning briefing from live data + LLM summary.

    If no LLM is available, returns a structured plain-text briefing.
    """
    # Gather facts
    s = db.stats()
    todos = db.todo_summary()
    proj_summ = projects.all_projects_summary()
    url_changes = [c for c in __import__("ops_dashboard.indexers.live_urls", fromlist=["status_changes"]).status_changes()
                   if c["delta"] in ("failed", "recovered")]
    url_failed = [c for c in url_changes if not c["ok"]]

    # Build a structured digest
    parts = []
    parts.append(f"# Briefing {time.strftime('%Y-%m-%d')}\n")
    parts.append("## Estado de tus 12 empresas\n")
    for p in proj_summ:
        url_health = (
            "🟢 all up" if p["all_urls_ok"] is True
            else ("🔴 some down" if p["all_urls_ok"] is False else "—")
        )
        last = time.strftime("%Y-%m-%d", time.localtime(p["last_activity"])) if p["last_activity"] else "—"
        parts.append(f"- **{p['name']}** ({p['key']}): {url_health} · {len(p['repos'])} repos · last activity {last} · {p['mention_count']} mentions")

    parts.append("\n## Live URLs\n")
    if url_failed:
        parts.append(f"⚠️ {len(url_failed)} URLs failing: " + ", ".join(c["name"] for c in url_failed))
    else:
        parts.append("✅ Todas las 13 URLs están operativas.")

    parts.append(f"\n## TODOs abiertos: {todos['open']} de {todos['total']} totales\n")
    by_p = [r for r in todos["by_project"] if r["project_key"]][:5]
    for r in by_p:
        parts.append(f"- {r['project_key']}: {r['c']} pendientes")

    parts.append(f"\n## Documentos indexados: {s['total']}\n")
    src_counts = ", ".join(f"{r['source']}: {r['c']}" for r in s["per_source"])
    parts.append(src_counts)

    digest = "\n".join(parts)

    # Optional: ask the LLM to summarize
    provider = resolve_provider()
    if provider.name != "mock":
        try:
            sys_prompt = (
                "You are the user's ops brain. Based on the structured data below, "
                "produce a 5-8 line morning briefing. Match the user's language. Be specific. "
                "Lead with what needs attention. Then a short 'all good' or 'issues' line."
            )
            summary = provider.ask(sys_prompt, [{"role": "user", "content": digest}])
            return summary + "\n\n---\n\n(datos crudos)\n" + digest
        except Exception as e:
            return digest + f"\n\n(LLM summary failed: {e})"
    return digest


# ---------- config helpers ----------

def get_config() -> dict:
    return _load_config()
