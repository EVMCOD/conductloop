"""Smarter Brain memory: semantic recall + auto-extraction of durable facts.

- ``recall(query)`` ranks the stored brain_memory entries by semantic similarity
  to a query (embeddings), so the Brain can pull the *relevant* memories into
  context instead of dumping all of them. Falls back to keyword overlap when the
  embedding model isn't installed.
- ``propose_from_text(text)`` heuristically extracts candidate durable facts from
  a conversation so the Brain can offer to remember them (like an assistant that
  learns). No LLM required.
"""
from __future__ import annotations

import logging
import re
import time

from . import db

log = logging.getLogger("conductloop.memory_ai")


def _keyword_score(query: str, text: str) -> float:
    q = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
    if not q:
        return 0.0
    t = set(re.findall(r"[a-z0-9]+", text.lower()))
    return len(q & t) / len(q)


def _cached_vectors(memories: list[dict]) -> dict[str, list[float]]:
    """Return {key: vector} for the given memories, computing + caching any that
    aren't indexed yet (memory_embeddings table). Only *new* memories embed, so
    repeat recalls embed just the query. Raises if the model is unavailable."""
    import json

    from . import embeddings
    with db.get_connection() as conn:
        rows = conn.execute("SELECT key, vector, indexed_at FROM memory_embeddings").fetchall()
        cached = {r["key"]: json.loads(r["vector"]) for r in rows}
        indexed_at = {r["key"]: r["indexed_at"] for r in rows}
        # (re)embed memories that are new OR were edited after they were indexed
        stale = [m for m in memories
                 if m["key"] not in cached or (m.get("updated_at") or 0) > indexed_at.get(m["key"], 0)]
        for m in stale:
            v = embeddings.embed(f"{m['key']}: {m['value']}")  # may raise -> keyword fallback
            cached[m["key"]] = v
            conn.execute(
                "INSERT INTO memory_embeddings (key, vector, model, indexed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET vector=excluded.vector, indexed_at=excluded.indexed_at",
                (m["key"], json.dumps(v), "all-MiniLM-L6-v2", int(time.time())),
            )
        if stale:
            conn.commit()
    return cached


def recall(query: str, limit: int = 5) -> list[dict]:
    """Return the most relevant memories to `query`, best first, with a score."""
    memories = db.memory_list()
    if not memories:
        return []
    scored = []
    try:
        from . import embeddings
        vectors = _cached_vectors(memories)          # embeds only new memories
        qv = embeddings.embed(query)                 # + the query, once
        for m in memories:
            mv = vectors.get(m["key"])
            if mv:
                scored.append((embeddings._cosine(qv, mv), m))
        method = "semantic"
    except Exception:
        # No model (or it failed) — keyword overlap fallback.
        for m in memories:
            scored.append((_keyword_score(query, f"{m['key']} {m['value']}"), m))
        method = "keyword"
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, m in scored[:limit]:
        if score <= 0 and method == "keyword":
            continue
        out.append({"key": m["key"], "value": m["value"], "score": round(float(score), 4), "method": method})
    return out


# Durable-fact cues: statements that tend to be worth remembering.
# English and Spanish (the Brain is told to match the user's language, so
# Spanish answers must be able to produce proposals too).
_FACT_CUE = re.compile(
    r"\b(is|are|uses?|prefers?|likes?|always|never|works? (on|at|with)|"
    r"lives? in|decided|chose|named|called|based (on|in)|runs? on"
    r"|es|son|est[aá]n?|soy|somos|estoy|estamos|usa(n|mos)?|uso|utiliza(n|mos)?|prefier[eo]n?|preferimos|"
    r"(me|le|les|nos) gustan?|siempre|nunca|jam[aá]s|trabaj(a|an|o|amos) (en|con|para)|"
    r"viv(e|en|o|imos) en|decid(imos|i[oó]|ieron|[ií])|(he|ha|hemos|han) decidido|"
    r"eleg(imos|[ií])|eligi(ó|o|eron)|escogimos|(se|me) llam(a|an|o)|llamad[oa]s?|"
    r"basad[oa]s? en|con sede en|corre en|funciona (en|con|sobre))\b",
    re.I,
)


def propose_from_text(text: str, *, max_items: int = 8) -> list[str]:
    """Extract candidate durable facts from free text (a chat, a note). Returns
    de-duplicated sentence-like candidates for the user to confirm + save."""
    # Split into sentence-ish chunks.
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    seen, out = set(), []
    for c in chunks:
        c = c.strip(" -*\t")
        if len(c) < 12 or len(c) > 240:
            continue
        if not _FACT_CUE.search(c):
            continue
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= max_items:
            break
    return out


def remember_proposals(proposals: list[str], *, prefix: str = "fact") -> int:
    """Persist confirmed proposals into brain_memory under auto keys."""
    import hashlib
    n = 0
    for p in proposals:
        h = hashlib.sha1(p.encode("utf-8")).hexdigest()[:8]
        db.memory_set(f"{prefix}:{h}", p)
        n += 1
    return n
