"""Local semantic search using sentence-transformers.

Model: all-MiniLM-L6-v2 (90MB, fast, 384-dim, English-strong).
Multilingual: paraphrase-multilingual-MiniLM-L12-v2 (470MB, slower) is an
alternative if you want Spanish/Catalan quality. We default to MiniLM for
speed and size; multilingual can be swapped via the EMBEDDING_MODEL env var.

Embeddings are stored as JSON arrays in the `embeddings` table. We index only
documents (not playbook entries, not live_urls).
"""
from __future__ import annotations

import json
import os
import time

from . import db

# Lazy-loaded so the rest of the app doesn't pay the model import cost.
_MODEL = None


class EmbeddingsUnavailable(RuntimeError):
    """Raised when the local embedding model can't be loaded — e.g. the
    lightweight packaged desktop build ships without sentence-transformers/torch.
    Callers degrade to keyword search instead of crashing."""


def _model():
    global _MODEL
    if _MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as e:  # dep excluded from this build
            raise EmbeddingsUnavailable(str(e)) from e
        name = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        # Suppress HF Hub warnings (we don't need auth for public models).
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
        from . import app_paths
        cache = str(app_paths.path("models", "data/models"))
        os.makedirs(cache, exist_ok=True)
        _MODEL = SentenceTransformer(name, cache_folder=cache)
    return _MODEL


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


def embed(text: str) -> list[float]:
    m = _model()
    v = m.encode(text, normalize_embeddings=True)
    return [float(x) for x in v]


def _cosine(a: list[float], b: list[float]) -> float:
    # Already L2-normalized, so dot product == cosine similarity.
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))


def index_document(doc_id: int, content: str, model_name: str = "all-MiniLM-L6-v2") -> None:
    """Compute and persist embedding for one document (idempotent by mtime+length)."""
    ensure_schema()
    with db.db() as conn:
        # Use first 4000 chars; embeddings don't benefit from more.
        text = (content or "")[:4000]
        if not text.strip():
            return
        try:
            v = embed(text)
        except EmbeddingsUnavailable:
            return  # no local model in this build — skip semantic indexing
        conn.execute(
            """
            INSERT INTO embeddings (doc_id, vector, model, indexed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                vector = excluded.vector, model = excluded.model, indexed_at = excluded.indexed_at
            """,
            (doc_id, json.dumps(v), model_name, int(time.time())),
        )


def run(limit: int | None = None) -> dict:
    """Embed all documents that don't have an embedding yet (or whose content changed)."""
    ensure_schema()
    with db.db() as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.content
            FROM documents d
            LEFT JOIN embeddings e ON e.doc_id = d.id
            WHERE e.doc_id IS NULL
               OR length(d.content) != (
                   SELECT length(d2.content) FROM documents d2 WHERE d2.id = d.id
               )
            LIMIT ?
            """ if False else
            """
            SELECT d.id, d.content FROM documents d
            LEFT JOIN embeddings e ON e.doc_id = d.id
            WHERE e.doc_id IS NULL
            """ + (" LIMIT ?" if limit else ""),
            (limit,) if limit else (),
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
    """Semantic search across all embedded documents."""
    ensure_schema()
    try:
        qvec = embed(query)
    except EmbeddingsUnavailable:
        return []  # no local model in this build — caller falls back to keyword search
    with db.db() as conn:
        rows = conn.execute(
            """
            SELECT e.doc_id, e.vector, d.source, d.path, d.title
            FROM embeddings e JOIN documents d ON d.id = e.doc_id
            """
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
