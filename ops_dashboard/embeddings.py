"""Semantic search over the documents.

Two engines, in this order:

1. **Local** — sentence-transformers ``all-MiniLM-L6-v2`` (90MB, 384-dim).
   Used in development, where the model can be installed.
2. **Remote** — the provider's embedding API (``embo-01``). The packaged
   desktop build deliberately ships without torch/sentence-transformers
   (hundreds of MB), so without this the app's "second brain" answered with
   file-tree stubs instead of search. 2026-10-23.

The engine decides which model the vectors belong to, and the vectors are
stored with that name: the two engines have different dimensions, so they
must never be compared with each other. Switching engine means re-indexing
(``make index``).

Embeddings are stored as JSON arrays in the `embeddings` table. We index only
documents (not playbook entries, not live_urls).
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

from . import db

log = logging.getLogger("conductloop.embeddings")

# Lazy-loaded so the rest of the app doesn't pay the model import cost.
_MODEL = None

LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
LOCAL_MODEL_TAG = "all-MiniLM-L6-v2"
REMOTE_MODEL = "embo-01"
REMOTE_MODEL_TAG = "minimax-embo-01"


class EmbeddingsUnavailable(RuntimeError):
    """Raised when no embedding engine is available — e.g. the lightweight
    packaged desktop build ships without sentence-transformers/torch and the
    user has no provider key configured. Callers degrade to keyword search
    instead of crashing."""


def _model():
    global _MODEL
    if _MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as e:  # dep excluded from this build
            raise EmbeddingsUnavailable(str(e)) from e
        name = os.environ.get("EMBEDDING_MODEL", LOCAL_MODEL)
        # Suppress HF Hub warnings (we don't need auth for public models).
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
        from . import app_paths
        cache = str(app_paths.path("models", "data/models"))
        os.makedirs(cache, exist_ok=True)
        _MODEL = SentenceTransformer(name, cache_folder=cache)
    return _MODEL


def _remote_config() -> tuple[str | None, str]:
    """(api_key, base_url) for the remote embedder, if the user has one."""
    try:
        from . import brain, byok_keys
        cfg = brain._load_config()
        key = byok_keys.get_api_key("minimax", cfg)
        base = (os.environ.get("MINIMAX_BASE_URL") or cfg.get("minimax_base_url")
                or "https://api.minimax.io").rstrip("/")
        # The chat endpoint is configured with the /anthropic suffix; embeddings
        # live at the API root.
        for suffix in ("/anthropic", "/v1"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        return key, base
    except Exception:
        return None, "https://api.minimax.io"


_REMOTE_COOLDOWN_UNTIL = 0.0
_REMOTE_COOLDOWN_SECONDS = 60


def _embed_remote(text: str, *, kind: str = "db") -> list[float]:
    """Embed through the provider API (MiniMax ``embo-01``).

    MiniMax does not use the OpenAI shape: the request carries ``texts`` and a
    ``type`` ("db" to index, "query" to search) and the answer is a top-level
    ``vectors`` array.
    """
    global _REMOTE_COOLDOWN_UNTIL
    if time.monotonic() < _REMOTE_COOLDOWN_UNTIL:
        raise EmbeddingsUnavailable(
            "embedding API is cooling down after a rate limit; keyword search for now"
        )
    key, base = _remote_config()
    if not key:
        raise EmbeddingsUnavailable(
            "no local embedding model and no provider key: semantic search is off"
        )
    body = json.dumps({"model": REMOTE_MODEL, "texts": [text], "type": kind}).encode()
    req = urllib.request.Request(
        f"{base}/v1/embeddings", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise EmbeddingsUnavailable(f"embedding API failed: {e}") from e
    vectors = data.get("vectors") or []
    if not vectors:
        base_resp = data.get("base_resp") or {}
        code = base_resp.get("status_code")
        msg = base_resp.get("status_msg") or str(data)[:200]
        if code == 1002 or "rate limit" in str(msg).lower():
            # Do not burn the quota retrying: back off for a minute.
            _REMOTE_COOLDOWN_UNTIL = time.monotonic() + _REMOTE_COOLDOWN_SECONDS
        raise EmbeddingsUnavailable(f"embedding API refused the request: {msg}") from None
    return [float(x) for x in vectors[0]]


def engine() -> str:
    """Which engine is in use: "provider", "local", "remote" or "none".

    The provider comes first, for the same reason :func:`embed` prefers
    it: the configured choice is the user's, and a development checkout
    that quietly used a different engine would index the world differently
    than the app does.
    """
    if _provider_engine() is not None:
        return "provider"
    try:
        _model()
        return "local"
    except EmbeddingsUnavailable:
        pass
    key, _ = _remote_config()
    return "remote" if key else "none"


def engine_model() -> str:
    """The tag stored with the vectors of the current engine."""
    kind = engine()
    if kind == "local":
        return LOCAL_MODEL_TAG
    if kind == "provider":
        provider = _provider_engine()
        return provider.embedding_tag() if provider else REMOTE_MODEL_TAG
    return REMOTE_MODEL_TAG



EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    doc_id     INTEGER PRIMARY KEY,
    vector     TEXT    NOT NULL,    -- JSON array of floats
    model      TEXT    NOT NULL,
    indexed_at INTEGER NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
);
"""


def ensure_schema() -> None:
    with db.db() as conn:
        conn.executescript(EXTRA_SCHEMA)


def embed(text: str, *, kind: str = "db") -> list[float]:
    """Embed a text with whichever engine is configured.

    The provider comes first, even where torch is installed: the whole
    point of configuring a provider is that the choice is the user's, and a
    development checkout that quietly used a different model than the app
    would index the world differently than the app does.
    """
    provider = _provider_engine()
    if provider is not None:
        try:
            vectors = provider.embed([text])
            if vectors:
                return [float(x) for x in vectors[0]]
        except Exception:
            # A provider that is set up but not answering must not be the
            # end of the road: the next engine may still work.
            log.debug("the embedding provider failed; trying the next engine",
                      exc_info=True)
    try:
        m = _model()
    except EmbeddingsUnavailable:
        return _embed_remote(text, kind=kind)
    v = m.encode(text, normalize_embeddings=True)
    return [float(x) for x in v]


def _provider_engine():
    """The first enabled provider that can embed, if there is one.

    A provider that is enabled but not answering is skipped: a search that
    fails because a local server is asleep is worse than one that uses the
    next engine.
    """
    try:
        from ops_dashboard import media_providers as mp

        for key in mp.enabled_keys(mp.EMBEDDINGS):
            try:
                provider = mp.build(key)
            except Exception:
                continue
            try:
                ok, _detail = provider.health()
            except Exception:
                ok = False
            if ok:
                return provider
    except Exception:
        return None
    return None


def _cosine(a: list[float], b: list[float]) -> float:
    # Already L2-normalized, so dot product == cosine similarity.
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))


def index_document(doc_id: int, content: str, model_name: str | None = None) -> None:
    """Compute and persist embedding for one document (idempotent by mtime+length)."""
    ensure_schema()
    with db.db() as conn:
        # Use first 4000 chars; embeddings don't benefit from more.
        text = (content or "")[:4000]
        if not text.strip():
            return
        try:
            v = embed(text, kind="db")
        except EmbeddingsUnavailable:
            return  # no engine in this build — skip semantic indexing
        conn.execute(
            """
            INSERT INTO embeddings (doc_id, vector, model, indexed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                vector = excluded.vector, model = excluded.model, indexed_at = excluded.indexed_at
            """,
            (doc_id, json.dumps(v), model_name or engine_model(), int(time.time())),
        )


def run(limit: int | None = None) -> dict:
    """Embed all documents that don't have an embedding yet (or whose content changed).

    A document whose vector was made with a *different* model is embedded
    again: the vectors of two models are not comparable, so keeping them
    would leave the search answering with the old model's view of the world.
    """
    ensure_schema()
    model = engine_model()
    with db.db() as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.content FROM documents d
            LEFT JOIN embeddings e ON e.doc_id = d.id
            WHERE e.doc_id IS NULL OR e.model IS NOT ?
            """ + (" LIMIT ?" if limit else ""),
            (model,) + ((limit,) if limit else ()),
        ).fetchall()

    n = 0
    t0 = time.perf_counter()
    for r in rows:
        try:
            index_document(r["id"], r["content"])
            n += 1
            if n % 50 == 0:
                elapsed = time.perf_counter() - t0
                print(f"  [{n}/{len(rows)}]  ({elapsed:.1f}s)")
        except Exception as e:
            print(f"  ! doc {r['id']}: {e}")
    print(f"embeddings: {n} new/updated in {time.perf_counter()-t0:.1f}s")
    return {"indexed": n}


def search(query: str, limit: int = 15) -> list[dict]:
    """Semantic search across all embedded documents.

    Only the vectors of the current engine are compared: the local model and
    the API model have different dimensions, so mixing them would give
    meaningless similarities.
    """
    ensure_schema()
    try:
        qvec = embed(query, kind="query")
    except EmbeddingsUnavailable:
        return []  # no engine in this build — caller falls back to keyword search
    model = engine_model()
    with db.db() as conn:
        rows = conn.execute(
            """
            SELECT e.doc_id, e.vector, d.source, d.path, d.title
            FROM embeddings e JOIN documents d ON d.id = e.doc_id
            WHERE e.model = ?
            """,
            (model,),
        ).fetchall()
    scored = []
    for r in rows:
        try:
            v = json.loads(r["vector"])
        except Exception:
            continue
        sim = _cosine(qvec, v)
        scored.append((sim, dict(r)))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {**row, "score": sim, "excerpt": ""}
        for sim, row in scored[:limit]
    ]
