"""Learning-to-rank over the documents the retrieval returns.

The retrieval scores a question against the vault with cosine similarity (and
FTS5 as a fallback). That score says "this text looks like the question". It
does not say "this is the note you actually wanted", because the only thing
that knows that is you: the citation you opened.

So this module keeps the click signal (``citation_events``) and learns a small
re-ranker on top of the retrieval score. It is a logistic regression over a
handful of features, written in plain Python on purpose: the desktop app is
frozen with PyInstaller and pulling in scikit-learn would add hundreds of
megabytes to the shipped binary for a model that fits in a few hundred bytes.

Two rules keep it honest:

* **A model that does not beat the baseline is never used.** The baseline is
  the retrieval score itself; the model has to beat it on a held-out split by
  a margin before it is allowed to touch the order of results.
* **Thin data moves nothing.** The blend weight grows with the number of
  samples, so a model trained on ten clicks can only nudge the order, never
  overturn it.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from pathlib import Path
from typing import Any

from .. import db

log = logging.getLogger("conductloop.ranker")

# Feature order is part of the stored model: changing it invalidates old rows.
FEATURE_NAMES = (
    "base_score",     # how well the text matched, normalised within the set
    "inv_rank",       # where the retrieval put it (1.0 = first)
    "clicks_prior",   # times this document was opened before
    "cited_prior",    # times this document was cited before
    "term_overlap",   # share of the question's words present in title+path
    "recency",        # how recent the document is
)

MIN_SAMPLES = 40        # rows needed before the model may be considered at all
MIN_POSITIVES = 8       # distinct opened citations needed
MIN_AUC_MARGIN = 0.02   # how much better than the baseline it has to be
BLEND_MAX = 0.7         # never give the model more than this much of the order
RETRAIN_EVERY = 20      # new clicks between automatic retrains

_WORD_RE = re.compile(r"[\wÀ-ÿ]{4,}", re.UNICODE)


class RankerError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _terms(text: Any) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def _clean_path(path: Any) -> str:
    """The path as the indexer stored it (trimmed).

    Both ``documents.path`` and the stored citations carry the path the
    indexer produced, so that exact string is the key that joins them.
    Resolving it would turn ``/tmp/x`` into ``/private/tmp/x`` on macOS and
    break the join, so the resolved form is only a second lookup key.
    """
    return path.strip() if isinstance(path, str) else ""


def _lookup_keys(path: str) -> list[str]:
    """The path and its resolved form, for lookups that may have either."""
    keys = [_clean_path(path)]
    if keys[0]:
        try:
            resolved = str(Path(keys[0]).expanduser().resolve())
            if resolved and resolved not in keys:
                keys.append(resolved)
        except (OSError, RuntimeError):
            pass
    return keys


# ---------- the signal ----------

def record_event(*, path: Any, source: Any = None, message_id: Any = None,
                 conversation_id: Any = None, event_kind: str = "open") -> dict:
    """Record that a citation was opened. Returns the stored row."""
    p = _clean_path(path)
    if not p:
        raise RankerError("bad_path", "path is required")
    if event_kind not in ("open", "copy", "followup"):
        raise RankerError("bad_kind", "event_kind must be open, copy or followup")
    try:
        mid = int(message_id) if message_id is not None else None
    except (TypeError, ValueError):
        mid = None
    try:
        cid = int(conversation_id) if conversation_id is not None else None
    except (TypeError, ValueError):
        cid = None
    with db.db() as conn:
        cur = conn.execute(
            "INSERT INTO citation_events (message_id, conversation_id, path, source, event_kind, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mid, cid, p, (source or None), event_kind, int(time.time())),
        )
        row_id = int(cur.lastrowid or 0)
    return {"id": row_id, "path": p, "source": source, "event_kind": event_kind,
            "message_id": mid, "conversation_id": cid}


def anchor_message(message_id: Any = None, conversation_id: Any = None) -> int | None:
    """The message that owns the citation list of an answer.

    A click arrives attached to the message the user was reading. In the team
    chat that is the agent's reply, but the citations of the turn belong to the
    plan message, and the Brain Hub stores them on the answer itself. Training
    needs the message that actually carries the citation list, so we look for
    the closest one before it in the same conversation.

    Returns None when there is nothing to attach the event to.
    """
    try:
        mid = int(message_id) if message_id is not None else None
    except (TypeError, ValueError):
        mid = None
    try:
        cid = int(conversation_id) if conversation_id is not None else None
    except (TypeError, ValueError):
        cid = None
    if mid is None and cid is None:
        return None
    with db.db() as conn:
        if mid is not None:
            row = conn.execute("SELECT conversation_id FROM messages WHERE id = ?", (mid,)).fetchone()
            if row is None:
                return None
            cid = int(row["conversation_id"])
            own = conn.execute(
                "SELECT id FROM messages WHERE id = ? AND citations IS NOT NULL AND citations != '[]'",
                (mid,)).fetchone()
            if own:
                return int(own["id"])
        if cid is None:
            return None
        if mid is not None:
            anchor = conn.execute(
                "SELECT id FROM messages WHERE conversation_id = ? AND id < ? "
                "AND citations IS NOT NULL AND citations != '[]' ORDER BY id DESC LIMIT 1",
                (cid, mid)).fetchone()
        else:
            anchor = conn.execute(
                "SELECT id FROM messages WHERE conversation_id = ? "
                "AND citations IS NOT NULL AND citations != '[]' ORDER BY id DESC LIMIT 1",
                (cid,)).fetchone()
    return int(anchor["id"]) if anchor else None


def events_since(ts: int) -> list[dict]:
    with db.db() as conn:
        rows = conn.execute(
            "SELECT id, message_id, conversation_id, path, source, event_kind, created_at "
            "FROM citation_events WHERE created_at >= ? ORDER BY id", (int(ts),)).fetchall()
    return [dict(r) for r in rows]


def count_events() -> int:
    with db.db() as conn:
        row = conn.execute("SELECT COUNT(*) FROM citation_events").fetchone()
    return int(row[0]) if row else 0


# ---------- the training set ----------

def _doc_meta(paths: set[str]) -> dict[str, dict]:
    """mtime and project for the given paths.

    Looked up by the path as stored and by its resolved form, because the
    indexer may have written either.
    """
    if not paths:
        return {}
    out: dict[str, dict] = {}
    with db.db() as conn:
        for p in paths:
            row = None
            for key in _lookup_keys(p):
                row = conn.execute(
                    "SELECT mtime, project_key, title FROM documents WHERE path = ? LIMIT 1", (key,)).fetchone()
                if row:
                    break
            if row:
                out[p] = {"mtime": row["mtime"], "project_key": row["project_key"], "title": row["title"]}
    return out


def _priors() -> tuple[dict[str, int], dict[str, int]]:
    """How often each path was opened, and how often it was cited.

    The citation count is counted by SQLite itself (json_each over the stored
    citations) rather than by pulling every answer into Python: this runs on
    the retrieval path, and the table only grows.
    """
    clicks: dict[str, int] = {}
    cited: dict[str, int] = {}
    with db.db() as conn:
        for r in conn.execute("SELECT path, COUNT(*) n FROM citation_events GROUP BY path").fetchall():
            clicks[r["path"]] = int(r["n"])
        try:
            rows = conn.execute(
                "SELECT json_extract(j.value, '$.path') AS p, COUNT(*) n "
                "FROM messages, json_each(messages.citations) j "
                "WHERE messages.citations IS NOT NULL AND messages.citations != '[]' "
                "GROUP BY p").fetchall()
        except Exception:
            rows = []
        for r in rows:
            if r["p"]:
                p = _clean_path(r["p"])
                cited[p] = cited.get(p, 0) + int(r["n"])
    return clicks, cited


def training_rows() -> list[dict]:
    """Build the ranking examples.

    An example is one retrieved document for one question, labelled 1 when the
    user opened it and 0 when they did not. The retrieved set of an answer is
    exactly its citation list, so the unopened citations of the same answer are
    the negatives. Questions come from the user message that preceded the
    answer in the same conversation.
    """
    clicks, cited = _priors()
    now = int(time.time())
    rows: list[dict] = []

    with db.db() as conn:
        # The role is not filtered: the message that owns the citation list is
        # the answer in the Brain Hub ('assistant') and the plan in the team
        # chat ('plan'), and both are worth learning from.
        answers = conn.execute(
            "SELECT id, conversation_id, citations FROM messages "
            "WHERE citations IS NOT NULL AND citations != '[]' "
            "ORDER BY id").fetchall()
        for a in answers:
            try:
                cites = json.loads(a["citations"])
            except (TypeError, ValueError):
                continue
            if not isinstance(cites, list) or not cites:
                continue
            qrow = conn.execute(
                "SELECT content FROM messages WHERE conversation_id = ? AND role = 'user' AND id < ? "
                "ORDER BY id DESC LIMIT 1", (a["conversation_id"], a["id"])).fetchone()
            if not qrow:
                continue
            question = qrow["content"] or ""
            opened = {r["path"] for r in conn.execute(
                "SELECT DISTINCT path FROM citation_events WHERE message_id = ?", (a["id"],)).fetchall()}
            for pos, c in enumerate(cites):
                if not isinstance(c, dict) or not c.get("path"):
                    continue
                p = _clean_path(c["path"])
                rows.append({
                    "question": question,
                    "path": p,
                    "label": 1 if p in opened else 0,
                    "position": pos,
                    "base_score": float(c.get("score") or 0.0),
                    "clicks_prior": clicks.get(p, 0),
                    "cited_prior": cited.get(p, 0),
                    "message_id": a["id"],
                })

    meta = _doc_meta({r["path"] for r in rows})
    for r in rows:
        m = meta.get(r["path"]) or {}
        r["mtime"] = m.get("mtime")
        r["title"] = m.get("title") or ""
        r["project_key"] = m.get("project_key")
        r["now"] = now
    return rows


# ---------- features ----------

def _features(row: dict) -> list[float]:
    """The feature vector for one example. Same order as FEATURE_NAMES."""
    qterms = _terms(row.get("question"))
    dterms = _terms((row.get("title") or "") + " " + (row.get("path") or ""))
    overlap = (len(qterms & dterms) / len(qterms)) if qterms else 0.0
    mtime = row.get("mtime")
    recency = 0.0
    if mtime:
        days = max(0.0, (row.get("now", int(time.time())) - float(mtime)) / 86400.0)
        recency = 1.0 / (1.0 + days / 30.0)
    return [
        float(row.get("base_score") or 0.0),
        float(row.get("inv_rank") or 0.0),
        math.log1p(float(row.get("clicks_prior") or 0)),
        math.log1p(float(row.get("cited_prior") or 0)),
        overlap,
        recency,
    ]


def _row_features(row: dict, n_in_set: int) -> list[float]:
    r = dict(row)
    r["inv_rank"] = 1.0 - (float(row.get("position") or 0) / max(1, n_in_set))
    return _features(r)


# ---------- the model ----------

def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _train_logistic(rows: list[list[float]], labels: list[int], *, epochs: int = 400,
                    lr: float = 0.25, l2: float = 0.01) -> tuple[list[float], float]:
    """Plain gradient descent on the logistic loss. Deterministic (no shuffle)."""
    n = len(rows)
    if not n:
        return [], 0.0
    d = len(rows[0])
    w = [0.0] * d
    b = 0.0
    for _ in range(epochs):
        gw = [0.0] * d
        gb = 0.0
        for x, y in zip(rows, labels):
            z = b + sum(wi * xi for wi, xi in zip(w, x))
            p = _sigmoid(z)
            err = p - y
            gb += err
            for i in range(d):
                gw[i] += err * x[i]
        for i in range(d):
            w[i] -= lr * (gw[i] / n + l2 * w[i])
        b -= lr * (gb / n)
    return w, b


def _auc(scores: list[float], labels: list[int]) -> float:
    """Rank-based AUC (ties get the average rank)."""
    pairs = sorted(zip(scores, labels), key=lambda t: t[0])
    n = len(pairs)
    if n < 2:
        return 0.5
    ranks: list[float] = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    pos = sum(1 for _, y in pairs if y == 1)
    neg = n - pos
    if pos == 0 or neg == 0:
        return 0.5
    rank_sum = sum(r for r, (_, y) in zip(ranks, pairs) if y == 1)
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def train(*, activate: bool = True) -> dict:
    """Train the re-ranker from the collected clicks.

    Returns a report. The model is only activated when it has enough samples
    and beats the retrieval score on a held-out split.
    """
    rows = training_rows()
    n = len(rows)
    positives = sum(1 for r in rows if r["label"] == 1)
    report: dict[str, Any] = {"samples": n, "positives": positives, "trained": False, "activated": False,
                               "auc": None, "baseline_auc": None, "reason": ""}
    if n < MIN_SAMPLES or positives < MIN_POSITIVES:
        report["reason"] = f"not enough data (need {MIN_SAMPLES} rows / {MIN_POSITIVES} positives)"
        return report

    # Split by answer, not by row: all the documents of one answer belong to
    # the same side, otherwise the model could learn the answer's own order.
    by_answer: dict[int, list[dict]] = {}
    for r in rows:
        by_answer.setdefault(r["message_id"], []).append(r)
    answers = sorted(by_answer)
    # Deterministic 80/20 split on the answer id.
    cut = max(1, int(len(answers) * 0.8))
    train_answers = set(answers[:cut])

    def _vecs(ans_set: set[int]) -> tuple[list[list[float]], list[int], list[float], list[int]]:
        xs, ys, base = [], [], []
        for aid in sorted(ans_set):
            group = by_answer[aid]
            n_in = len(group)
            for r in group:
                xs.append(_row_features(r, n_in))
                ys.append(int(r["label"]))
                base.append(float(r.get("base_score") or 0.0))
        return xs, ys, base, ys

    tr_x, tr_y, _, _ = _vecs(train_answers)
    te_x, te_y, te_base, _ = _vecs(set(answers) - train_answers)

    if len(tr_x) < 10 or len(set(tr_y)) < 2 or len(set(te_y)) < 2:
        report["reason"] = "the split has no positives or negatives to learn from"
        return report

    w, b = _train_logistic(tr_x, tr_y)
    model_scores = [_sigmoid(b + sum(wi * xi for wi, xi in zip(w, x))) for x in te_x]
    model_auc = _auc(model_scores, te_y)
    base_auc = _auc(te_base, te_y)
    report["auc"] = round(model_auc, 4)
    report["baseline_auc"] = round(base_auc, 4)
    report["trained"] = True

    with db.db() as conn:
        version = int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM ranker_model").fetchone()[0]) + 1
        conn.execute("UPDATE ranker_model SET is_active = 0")
        conn.execute(
            "INSERT INTO ranker_model (model, version, feature_names, weights, bias, samples, "
            "positives, auc, trained_at, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("logistic", version, json.dumps(list(FEATURE_NAMES)), json.dumps(w), float(b),
             len(tr_x), sum(tr_y), model_auc, int(time.time()),
             1 if (activate and model_auc >= base_auc + MIN_AUC_MARGIN) else 0),
        )
        if not (activate and model_auc >= base_auc + MIN_AUC_MARGIN):
            report["reason"] = (f"does not beat the baseline on held-out data "
                                f"(model {model_auc:.3f} vs base {base_auc:.3f})")
    if activate and report["reason"] == "":
        report["activated"] = True
    return report


def active_model() -> dict | None:
    with db.db() as conn:
        row = conn.execute(
            "SELECT id, version, feature_names, weights, bias, samples, positives, auc, trained_at "
            "FROM ranker_model WHERE is_active = 1 ORDER BY version DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        names = json.loads(row["feature_names"])
        weights = json.loads(row["weights"])
    except (TypeError, ValueError):
        return None
    if len(names) != len(weights) or len(names) != len(FEATURE_NAMES):
        return None
    return {"id": row["id"], "version": row["version"], "feature_names": names,
            "weights": [float(w) for w in weights], "bias": float(row["bias"]),
            "samples": int(row["samples"]), "positives": int(row["positives"]),
            "auc": row["auc"], "trained_at": row["trained_at"]}


def model_status() -> dict:
    """What the ranker knows right now. Safe to call from the UI."""
    events = count_events()
    m = active_model()
    return {
        "events": events,
        "trained": bool(m),
        "samples": m["samples"] if m else 0,
        "positives": m["positives"] if m else 0,
        "auc": m["auc"] if m else None,
        "version": m["version"] if m else None,
        "min_samples": MIN_SAMPLES,
        "min_positives": MIN_POSITIVES,
        "feature_names": list(FEATURE_NAMES),
        "weights": m["weights"] if m else None,
        "bias": m["bias"] if m else None,
    }


# ---------- applying it ----------

def rerank(question: str, candidates: list[dict], *, project_key: str | None = None) -> list[dict]:
    """Re-order the retrieved candidates with the learned model.

    Returns the list unchanged when there is no model, or when the model is not
    allowed to move the order much (thin data). Never raises: a broken ranker
    must not break the Brain.
    """
    if not candidates or len(candidates) < 2:
        return candidates
    model = active_model()
    if not model:
        return candidates
    try:
        n = len(candidates)
        clicks, cited = _priors()
        meta = _doc_meta({str(c.get("path") or "") for c in candidates})
        now = int(time.time())
        base_scores = [float(c.get("score") or 0.0) for c in candidates]
        bmin, bmax = (min(base_scores), max(base_scores)) if base_scores else (0.0, 0.0)
        span = (bmax - bmin) or 1.0

        feats: list[list[float]] = []
        for i, c in enumerate(candidates):
            p = _clean_path(c.get("path"))
            m = meta.get(p) or {}
            row = {
                "question": question,
                "title": m.get("title") or c.get("title") or "",
                "path": p,
                "mtime": m.get("mtime"),
                "now": now,
                "base_score": (base_scores[i] - bmin) / span,
                "inv_rank": 1.0 - (i / n),
                "clicks_prior": clicks.get(p, 0),
                "cited_prior": cited.get(p, 0),
            }
            feats.append(_features(row))

        model_scores = [_sigmoid(model["bias"] + sum(wi * xi for wi, xi in zip(model["weights"], x)))
                        for x in feats]
        # Blend with the retrieval order: thin data may only nudge it.
        alpha = min(BLEND_MAX, model["samples"] / 150.0)
        scored = []
        for i, c in enumerate(candidates):
            base_norm = 1.0 - (i / n)
            final = (1.0 - alpha) * base_norm + alpha * model_scores[i]
            scored.append((final, i, c))
        scored.sort(key=lambda t: (-t[0], t[1]))
        out = []
        for _, i, c in scored:
            item = dict(c)
            item["rerank_score"] = round(model_scores[i], 6)
            item["rerank_base_rank"] = i + 1
            out.append(item)
        return out
    except Exception:
        log.debug("rerank failed, keeping the retrieval order", exc_info=True)
        return candidates
