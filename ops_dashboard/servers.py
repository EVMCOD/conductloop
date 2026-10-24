"""What the product can talk to, and to whom.

The product asks for capabilities, not for vendors: chat, image, video,
speech, embeddings. Each of them can be served by a service in the cloud,
by a server running on this Mac, or by a binary that is already
installed.

This puts all of them in one place with their real state. The question is
not "which vendor did you pick" but "what is going to answer when I press
the button" — and that answer changes: a local server is stopped, a key
expires, a model gets downloaded.

Two reads, on purpose:

* ``status()`` reads the configuration only: who is set up, where they
  point, where the key lives. It never waits for a network, so a page that
  shows it opens instantly.
* ``probe()`` asks the servers themselves. It is slow by nature — a server
  that is not there takes seconds to say so — so it is asked for, not paid
  for by every page that opens.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
from typing import Any

log = logging.getLogger("conductloop.servers")

CAPABILITIES = (
    ("chat", "Chat", "what the brain thinks with"),
    ("image", "Image", "pictures out of a sentence"),
    ("video", "Video", "motion out of a sentence"),
    ("speech", "Speech", "a voice for the words"),
    ("embeddings", "Embeddings", "the search that answers with no network"),
)

# The brain's own servers, in the order the user is offered them.
CHAT_SERVERS = ("minimax", "anthropic", "openai", "gateway", "ollama")

LABELS = {
    "minimax": "MiniMax",
    "anthropic": "Anthropic",
    "openai": "OpenAI-compatible",
    "gateway": "Gateway",
    "ollama": "Ollama",
    "local_onnx": "Local embeddings (ONNX)",
}


def _is_local(url: str) -> bool:
    """Is this endpoint on this machine?

    Local work is free, private and has no rate limit, so a local server is
    used before a cloud one — the same rule the media providers follow.
    """
    if not url:
        return False
    host = url.split("//")[-1].split("/")[0].split(":")[0].strip().lower()
    if not host:
        return False
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or host.endswith(".local")


# ── the chat servers ───────────────────────────────────────────────────────

def _chat_server(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """One chat server: where it points, and where its key lives."""
    from . import byok_keys

    base = ""
    model = ""
    needs_key = True
    if name == "anthropic":
        base = str(os.environ.get("ANTHROPIC_BASE_URL") or cfg.get("anthropic_base_url") or "")
        model = str(os.environ.get("BRAIN_MODEL") or cfg.get("anthropic_model") or "")
    elif name == "openai":
        base = str(os.environ.get("OPENAI_BASE_URL") or cfg.get("openai_base_url") or "")
        model = str(os.environ.get("BRAIN_MODEL") or cfg.get("openai_model") or "")
    elif name == "minimax":
        base = str(os.environ.get("MINIMAX_BASE_URL") or cfg.get("minimax_base_url") or "")
        model = str(os.environ.get("BRAIN_MODEL") or cfg.get("minimax_model") or "")
    elif name == "gateway":
        base = str(os.environ.get("GATEWAY_BASE_URL") or cfg.get("gateway_base_url") or "")
        model = str(os.environ.get("BRAIN_MODEL") or cfg.get("gateway_model") or "")
    else:
        # Ollama runs here: it needs no key, only to be there.
        base = str(os.environ.get("OLLAMA_BASE_URL") or cfg.get("ollama_base_url")
                   or "http://localhost:11434")
        model = str(os.environ.get("OLLAMA_MODEL") or cfg.get("ollama_model") or "")
        needs_key = False

    storage = byok_keys.key_storage(name, cfg) if needs_key else None
    return {
        "name": name,
        "label": LABELS.get(name, name),
        "where": base,
        "model": model,
        "local": _is_local(base),
        "key": storage,
        "needs_key": needs_key,
        "configured": bool(storage) if needs_key else True,
    }


def _chat() -> dict[str, Any]:
    """The brain's servers: who is set up, and which one is the default."""
    from . import brain

    cfg = brain._load_config()
    chosen = str(cfg.get("default_provider") or "").strip().lower()
    servers = [_chat_server(name, cfg) for name in CHAT_SERVERS]
    active = next((s for s in servers if s["name"] == chosen), None)
    cooling = brain._provider_cooldown_remaining(chosen) if chosen else 0
    # The brain falls back on its own when the default has no key: the first
    # one that is set up answers. A local server without a key does not count
    # here — "configured" for it only means "it would answer if it were
    # running", and the panel does not pretend otherwise.
    fallback = next((s for s in servers
                     if s["name"] != chosen and s.get("key")), None)
    state = "ready"
    if active is None or not active["configured"]:
        state = "unconfigured"
    elif cooling:
        state = "cooling"
    return {
        "active": active,
        "fallback": fallback,
        "servers": servers,
        "state": state,
        "cooling": cooling,
        "note": "The brain asks for a model, not for a company: point it at any "
                "OpenAI-compatible server and it will use that one.",
    }


# ── the media servers ──────────────────────────────────────────────────────

def _media(capability: str) -> dict[str, Any]:
    """The providers that can serve a capability, and the one that will."""
    from . import media

    catalogue = {row["key"]: row for row in media.providers.catalogue()}
    enabled = media.providers.enabled_keys(capability)
    servers = []
    for key, row in catalogue.items():
        if capability not in (row.get("capabilities") or []):
            continue
        servers.append({
            "name": key,
            "label": row.get("label") or key,
            "where": row.get("effective_base_url") or "",
            "model": str((row.get("default_config") or {}).get("model") or ""),
            "local": row.get("kind") == "local",
            "key": "stored" if row.get("configured") else None,
            "needs_key": bool(row.get("needs_key")),
            "configured": bool(row.get("enabled")) and bool(row.get("configured")),
            "enabled": bool(row.get("enabled")),
        })
    active = next((s for s in servers if s["name"] == enabled[0]), None) if enabled else None
    state = "ready" if active is not None else "unconfigured"
    return {
        "active": active,
        "servers": servers,
        "state": state,
        "note": "Local first: when a provider that runs on this Mac is set up, "
                "it is used before the cloud ones.",
    }


# ── the embeddings ─────────────────────────────────────────────────────────

def _embeddings() -> dict[str, Any]:
    """Where the vectors come from, and how much of the vault is in there."""
    from . import db, embeddings

    kind = embeddings.engine()
    model = embeddings.engine_model()
    servers: list[dict[str, Any]] = []
    active = None
    if kind == "provider":
        from . import media

        enabled = media.providers.enabled_keys(media.providers.EMBEDDINGS)
        catalogue = {row["key"]: row for row in media.providers.catalogue()}
        for key in enabled:
            row = catalogue.get(key) or {}
            entry = {
                "name": key,
                "label": row.get("label") or LABELS.get(key, key),
                "where": row.get("effective_base_url") or "",
                "model": str((row.get("default_config") or {}).get("model") or ""),
                "local": row.get("kind") == "local",
                "key": "stored" if row.get("configured") else None,
                "needs_key": bool(row.get("needs_key")),
                "configured": bool(row.get("enabled")) and bool(row.get("configured")),
                "enabled": bool(row.get("enabled")),
            }
            servers.append(entry)
            if active is None:
                active = entry
    try:
        with db.db() as conn:
            indexed = int(conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])
            total = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    except Exception as e:
        log.warning("could not count the embeddings: %s", e)
        indexed = total = 0

    state = "ready" if kind != "none" else "unconfigured"
    return {
        "active": active or {
            "name": kind or "none",
            "label": {"local": "Local model", "remote": "Remote API",
                      "provider": "Provider", "none": "Nothing"}.get(kind, kind),
            "where": "", "model": model, "local": kind == "local",
            "key": None, "needs_key": False, "configured": kind != "none",
        },
        "servers": servers,
        "state": state,
        "indexed": indexed,
        "total": total,
        "note": "The search answers without a network, a key or a credit "
                "when a local model is set up.",
    }


# ── the whole picture ──────────────────────────────────────────────────────

def status() -> dict[str, Any]:
    """Every capability, the server that will serve it, and its state.

    Configuration only: this never waits for a network, so the page that
    shows it opens as fast as the app does.
    """
    rows = []
    for key, label, what in CAPABILITIES:
        if key == "chat":
            body = _chat()
        elif key == "embeddings":
            body = _embeddings()
        else:
            body = _media(key)
        rows.append({"key": key, "label": label, "what": what, **body})
    return {"capabilities": rows}


# ── asking the servers ─────────────────────────────────────────────────────

def _probe_media(key: str) -> dict[str, Any]:
    """Ask one media provider whether it is there."""
    from . import media

    try:
        provider = media.providers.build(key)
    except Exception as e:
        return {"ok": False, "message": str(e)[:200]}
    try:
        ok, message = provider.health()
    except Exception as e:
        return {"ok": False, "message": str(e)[:200]}
    return {"ok": bool(ok), "message": str(message)[:200]}


def _probe_chat(server: dict[str, Any]) -> dict[str, Any]:
    """Is the chat server there?

    A local server is asked. A remote one is not: the only honest way to
    know that a key works is to spend a request on it, and this panel does
    not spend the user's money to decorate a status line.
    """
    if not server.get("local"):
        where = server.get("key")
        return {"ok": bool(where),
                "message": f"the key is in the {where}" if where else "no key is stored"}
    url = str(server.get("where") or "").rstrip("/")
    if not url:
        return {"ok": False, "message": "no local server is configured"}
    try:
        from . import media_backends  # noqa: F401  (the http helper lives there)
        from .media_providers import http_json

        data = http_json(f"{url}/models", timeout=6)
        models = data.get("data") if isinstance(data, dict) else None
        count = len(models) if isinstance(models, list) else 0
        return {"ok": True, "message": f"{url} is answering ({count} model(s))"}
    except Exception as e:
        return {"ok": False, "message": str(e)[:200]}


def probe(timeout: float = 12.0) -> dict[str, Any]:
    """Ask every server whether it is there.

    Slow by nature: a server that is not there takes seconds to say so.
    The answers are asked for in parallel so the wait is the slowest
    server, not the sum of all of them.
    """
    targets: list[tuple[str, str, dict[str, Any]]] = []
    for capability, servers in _probe_targets():
        for server in servers:
            targets.append((capability, server["name"], server))

    out: dict[str, dict[str, Any]] = {}
    if not targets:
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        futures = {}
        for capability, name, server in targets:
            if capability == "chat":
                futures[pool.submit(_probe_chat, server)] = (capability, name)
            else:
                futures[pool.submit(_probe_media, name)] = (capability, name)
        for future, (capability, name) in futures.items():
            try:
                result = future.result(timeout=timeout)
            except Exception as e:
                result = {"ok": False, "message": str(e)[:200]}
            out.setdefault(capability, {})[name] = result
    return out


def _probe_targets() -> list[tuple[str, list[dict[str, Any]]]]:
    """The servers worth asking, per capability."""
    from . import media

    rows: list[tuple[str, list[dict[str, Any]]]] = []
    for capability in (media.providers.IMAGE, media.providers.VIDEO,
                        media.providers.SPEECH, media.providers.EMBEDDINGS):
        body = _media(capability)
        rows.append((capability, [s for s in body["servers"] if s.get("enabled")]))
    rows.append(("chat", [s for s in _chat()["servers"] if s.get("configured")]))
    return rows
