"""SQLite store for feedback, the opt-in failure log and learning settings.

Tables come from migration 9 (``migrations._mig_9_learning_feedback``).
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from .. import db
from ..fileperms import restrict_to_owner

log = logging.getLogger("conductloop.learning")

FEEDBACK_TARGETS = ("brain_message", "boardroom_message", "agent_run", "session_task")
# Targets that can be validated on this branch; the others are accepted by the
# schema for later PRs (sessions review hook, Boardroom) but rejected by the API.
SUPPORTED_TARGETS = ("brain_message",)
FEEDBACK_REASONS = ("wrong", "missing_citation", "bad_citation", "outdated", "style", "unsafe", "other")
FAILURE_KINDS = ("provider_error", "timeout", "broken_citation", "tool_error", "budget", "eval_regression")
RETENTION_DAYS = 30  # D6
MAX_COMMENT = 2000
MAX_ANSWER_MATCH = 200_000

_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:\-]{0,63}$")
_SETTING_FAILURE_LOG = "failure_log.enabled"


class LearningError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ---------- settings ----------

def _get_setting(key: str, default: str) -> str:
    try:
        with db.db() as conn:
            row = conn.execute("SELECT value FROM learning_settings WHERE key = ?", (key,)).fetchone()
    except Exception:
        return default
    return str(row["value"]) if row else default


def _set_setting(key: str, value: str) -> None:
    with db.db() as conn:
        conn.execute(
            "INSERT INTO learning_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, int(time.time())),
        )


def failure_log_enabled() -> bool:
    """D6: off unless the user switched it on."""
    return _get_setting(_SETTING_FAILURE_LOG, "0") == "1"


def set_failure_log_enabled(enabled: bool) -> None:
    _set_setting(_SETTING_FAILURE_LOG, "1" if enabled else "0")
    if not enabled:
        # Turning it off also forgets what was logged.
        clear_failures()


# ---------- feedback (D7) ----------

def resolve_brain_message(*, message_id: Any = None, conversation_id: Any = None,
                          answer: Any = None) -> int:
    """Find the assistant message a 👍/👎 refers to.

    By id, or (for the streaming Brain Hub, which does not receive ids) by the
    latest assistant message with exactly that answer text, optionally within a
    conversation."""
    with db.db() as conn:
        if message_id is not None:
            try:
                mid = int(message_id)
            except (TypeError, ValueError) as e:
                raise LearningError("bad_target", "message_id must be an integer") from e
            row = conn.execute("SELECT id, role FROM messages WHERE id = ?", (mid,)).fetchone()
            if not row or row["role"] != "assistant":
                raise LearningError("target_not_found", "assistant message not found", 404)
            return int(row["id"])
        if not isinstance(answer, str) or not answer.strip():
            raise LearningError("bad_target", "message_id or answer is required")
        if len(answer) > MAX_ANSWER_MATCH:
            raise LearningError("bad_target", "answer too long")
        # The UI sends the bubble text; tolerate surrounding whitespace only.
        sql = "SELECT id FROM messages WHERE role = 'assistant' AND (content = ? OR trim(content) = ?)"
        params: list[Any] = [answer, answer.strip()]
        if conversation_id is not None:
            try:
                params.append(int(conversation_id))
            except (TypeError, ValueError) as e:
                raise LearningError("bad_target", "conversation_id must be an integer") from e
            sql += " AND conversation_id = ?"
        row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
        if not row:
            raise LearningError("target_not_found", "assistant message not found", 404)
        return int(row["id"])


def record_feedback(*, target_kind: str, target_id: int, rating: int,
                    reason_code: str | None = None, comment: str | None = None) -> dict:
    """Upsert one vote per target. Returns the stored row."""
    if target_kind not in SUPPORTED_TARGETS:
        raise LearningError("unsupported_target", f"target_kind must be one of {SUPPORTED_TARGETS}")
    if rating not in (-1, 1) or isinstance(rating, bool):
        raise LearningError("bad_rating", "rating must be 1 or -1")
    if reason_code is not None and reason_code not in FEEDBACK_REASONS:
        raise LearningError("bad_reason", f"reason_code must be one of {FEEDBACK_REASONS}")
    if rating == 1:
        reason_code = None  # reasons explain a 👎
    if comment is not None:
        if not isinstance(comment, str):
            raise LearningError("bad_comment", "comment must be a string")
        comment = comment.strip()[:MAX_COMMENT] or None
    now = int(time.time())
    with db.db() as conn:
        msg = conn.execute("SELECT provider, model FROM messages WHERE id = ? AND role = 'assistant'",
                           (target_id,)).fetchone()
        if not msg:
            raise LearningError("target_not_found", "assistant message not found", 404)
        conn.execute(
            "INSERT INTO feedback (target_kind, target_id, rating, reason_code, comment, provider, model,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target_kind, target_id) DO UPDATE SET rating = excluded.rating,"
            " reason_code = excluded.reason_code, comment = excluded.comment, updated_at = excluded.updated_at",
            (target_kind, target_id, rating, reason_code, comment, msg["provider"], msg["model"], now, now),
        )
        row = conn.execute("SELECT * FROM feedback WHERE target_kind = ? AND target_id = ?",
                           (target_kind, target_id)).fetchone()
    return dict(row)


def delete_feedback(feedback_id: int) -> bool:
    with db.db() as conn:
        return conn.execute("DELETE FROM feedback WHERE id = ?", (feedback_id,)).rowcount > 0


def list_feedback(*, limit: int = 50, rating: int | None = None) -> list[dict]:
    """Newest first, with a short excerpt of the rated answer (local UI only)."""
    sql = ("SELECT f.*, substr(m.content, 1, 240) AS answer_excerpt, m.conversation_id "
           "FROM feedback f LEFT JOIN messages m ON f.target_kind = 'brain_message' AND m.id = f.target_id")
    params: list[Any] = []
    if rating in (-1, 1):
        sql += " WHERE f.rating = ?"
        params.append(rating)
    sql += " ORDER BY f.updated_at DESC, f.id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with db.db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---------- failure log (D6) ----------

def _clip(value: Any, n: int) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s[:n] or None


def record_failure(kind: str, code: str, *, provider: str | None = None, model: str | None = None,
                   route: str | None = None) -> bool:
    """Log one failure **code** if the user enabled the failure log.

    Never raises and never stores free text: ``code`` must look like an error
    code (``[a-z0-9_.:-]``, ≤ 64 chars) or it is stored as ``other``."""
    try:
        if kind not in FAILURE_KINDS or not failure_log_enabled():
            return False
        code = code if isinstance(code, str) and _CODE_RE.match(code) else "other"
        now = int(time.time())
        with db.db() as conn:
            conn.execute(
                "INSERT INTO failure_log (kind, code, provider, model, route, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (kind, code, _clip(provider, 64), _clip(model, 128), _clip(route, 128), now),
            )
            conn.execute("DELETE FROM failure_log WHERE created_at < ?", (now - RETENTION_DAYS * 86400,))
        return True
    except Exception:
        log.debug("record_failure failed", exc_info=True)
        return False


def purge_expired(now: int | None = None) -> int:
    now = int(time.time()) if now is None else now
    with db.db() as conn:
        return conn.execute("DELETE FROM failure_log WHERE created_at < ?",
                            (now - RETENTION_DAYS * 86400,)).rowcount


def list_failures(*, limit: int = 100) -> list[dict]:
    purge_expired()
    with db.db() as conn:
        rows = conn.execute("SELECT * FROM failure_log ORDER BY created_at DESC, id DESC LIMIT ?",
                            (max(1, min(int(limit), 1000)),)).fetchall()
    return [dict(r) for r in rows]


def clear_failures() -> int:
    with db.db() as conn:
        return conn.execute("DELETE FROM failure_log").rowcount


# ---------- summary / export ----------

def summary() -> dict:
    purge_expired()
    with db.db() as conn:
        fb = conn.execute(
            "SELECT COALESCE(SUM(rating = 1), 0) AS up, COALESCE(SUM(rating = -1), 0) AS down FROM feedback"
        ).fetchone()
        reasons = conn.execute(
            "SELECT reason_code, COUNT(*) AS n FROM feedback WHERE rating = -1 AND reason_code IS NOT NULL "
            "GROUP BY reason_code ORDER BY n DESC"
        ).fetchall()
        fails = conn.execute(
            "SELECT kind, code, COUNT(*) AS n FROM failure_log GROUP BY kind, code ORDER BY n DESC LIMIT 20"
        ).fetchall()
    return {
        "feedback": {"up": int(fb["up"]), "down": int(fb["down"]),
                     "down_reasons": {r["reason_code"]: int(r["n"]) for r in reasons}},
        "failure_log": {"enabled": failure_log_enabled(), "retention_days": RETENTION_DAYS,
                        "top": [dict(r) for r in fails]},
    }


def export_dir() -> Path:
    return Path(db.DB_PATH).parent / "learning-exports"


def export_local(*, include_comments: bool = False) -> Path:
    """Write an anonymised JSON export to the local data dir and return its path.

    No answer text, no conversation ids, no paths; comments only if asked."""
    purge_expired()
    with db.db() as conn:
        fb = conn.execute(
            "SELECT target_kind, rating, reason_code, comment, provider, model, created_at FROM feedback "
            "ORDER BY created_at"
        ).fetchall()
        fl = conn.execute("SELECT kind, code, provider, model, route, created_at FROM failure_log "
                          "ORDER BY created_at").fetchall()
    feedback = []
    for r in fb:
        d = dict(r)
        if not include_comments:
            d.pop("comment", None)
        feedback.append(d)
    data = {"format": "conductloop-learning-export", "version": 1, "exported_at": int(time.time()),
            "feedback": feedback, "failures": [dict(r) for r in fl]}
    out_dir = export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / time.strftime("learning-export-%Y%m%d-%H%M%S.json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    restrict_to_owner(path)
    return path
