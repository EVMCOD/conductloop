""""¿Estoy aprendiendo bien?" — local learning metrics (design doc §7, PR L8).

Everything is computed from the local SQLite on demand; nothing is sent
anywhere. With one user the numbers are a *trend*, not statistics: every block
carries its sample size and the report flags small samples.

* 👎 per 100 Brain answers, overall and split by "used lessons" / "no lessons"
  (answers are matched to ``lesson_uses`` by sha256, never by text).
* Optional local A/B (OFF by default): a configurable share of answers that
  *would* get lessons is answered without them (``learning_ab``), so the
  comparison is between comparable answers.
* Lessons: usage, 👎 on answers that used each lesson, % archived.
* Skills: activations, proposal outcomes, unused (60 days); the with/without
  skill pass-rate delta needs live evals (L2) and is reported as missing.
* Sessions (#18, if present): tasks accepted on the first attempt, before and
  after the first approved lesson.
* Offline evals: baseline pass rate per suite (read-only, L7).

The scheduled curator (D14) only *proposes* archiving skills unused for 60
days. It never moves or deletes anything; approving is the user's action.
"""
from __future__ import annotations

import hashlib
import logging
import random
import time
from typing import Any

from .. import db
from .store import LearningError, _get_setting, _set_setting

log = logging.getLogger(__name__)

SETTING_AB = "metrics.ab_holdout_pct"
SETTING_CURATOR = "skills.curator_enabled"
AB_MAX_PCT = 50
DEFAULT_DAYS = 30
MIN_SAMPLE = 20           # below this, the report says "too little signal"
MAX_ANSWERS_SCANNED = 20_000
TREND_POINTS = 5.0        # 👎/100 change considered a real move


# ---------- settings ----------

def ab_holdout_pct() -> int:
    try:
        v = int(_get_setting(SETTING_AB, "0"))
    except ValueError:
        return 0
    return max(0, min(AB_MAX_PCT, v))


def ab_holdout() -> bool:
    """True if this answer should go without lessons (A/B holdout arm)."""
    pct = ab_holdout_pct()
    return pct > 0 and random.random() * 100 < pct  # noqa: S311 (not security-relevant)


def curator_enabled() -> bool:
    return _get_setting(SETTING_CURATOR, "1") == "1"


def settings() -> dict:
    return {"ab_holdout_pct": ab_holdout_pct(), "ab_max_pct": AB_MAX_PCT, "curator_enabled": curator_enabled()}


def update_settings(data: dict) -> dict:
    if "ab_holdout_pct" in data:
        v = data["ab_holdout_pct"]
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= AB_MAX_PCT:
            raise LearningError("bad_value", f"ab_holdout_pct must be an integer 0-{AB_MAX_PCT}")
        _set_setting(SETTING_AB, str(v))
    if "curator_enabled" in data:
        if not isinstance(data["curator_enabled"], bool):
            raise LearningError("bad_value", "curator_enabled must be true/false")
        _set_setting(SETTING_CURATOR, "1" if data["curator_enabled"] else "0")
    return settings()


# ---------- helpers ----------

def _sha(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def _table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _rate(down: int, answers: int) -> dict:
    return {"answers": answers, "down": down,
            "per_100": round(down * 100.0 / answers, 2) if answers else None,
            "enough": answers >= MIN_SAMPLE}


def _answers(conn, since: int, until: int) -> list[tuple[int, str]]:
    rows = conn.execute("SELECT id, content FROM messages WHERE role = 'assistant' AND created_at >= ? "
                        "AND created_at < ? ORDER BY id DESC LIMIT ?", (since, until, MAX_ANSWERS_SCANNED))
    return [(int(r["id"]), _sha(r["content"])) for r in rows]


def _match_by_sha(answers: list[tuple[int, str]], events: list[tuple[str, Any]]) -> dict[int, Any]:
    """Pair each answer with at most one event of the same text hash, both in
    creation order. Identical answers (same sha) then keep their own A/B arm /
    lesson use instead of all inheriting one of them."""
    queues: dict[str, list[Any]] = {}
    for sha, value in events:                      # events: oldest first
        queues.setdefault(sha, []).append(value)
    out: dict[int, Any] = {}
    for mid, sha in sorted(answers):               # message ids grow with time
        q = queues.get(sha)
        if q:
            out[mid] = q.pop(0)
    return out


# ---------- blocks ----------

def downvotes(since: int, until: int) -> dict:
    with db.db() as conn:
        answers = _answers(conn, since, until)
        ids = [a for a, _ in answers]
        down_ids: set[int] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            down_ids |= {int(r[0]) for r in conn.execute(
                f"SELECT target_id FROM feedback WHERE target_kind = 'brain_message' AND rating = -1 "  # noqa: S608
                f"AND target_id IN ({marks})", chunk)}
        # One lesson_uses row per lesson: one "use" per (answer, moment).
        uses = [(r[0], True) for r in conn.execute(
            "SELECT answer_sha, created_at FROM lesson_uses WHERE created_at >= ? "
            "GROUP BY answer_sha, created_at ORDER BY created_at", (since - 86400,))] \
            if _table(conn, "lesson_uses") else []
        arm_events = [(r[0], r[1]) for r in conn.execute(
            "SELECT answer_sha, arm FROM learning_ab WHERE created_at >= ? ORDER BY created_at, rowid",
            (since - 86400,))] if _table(conn, "learning_ab") else []
    used_by = _match_by_sha(answers, uses)
    arm_by = _match_by_sha(answers, arm_events)
    with_l = [(a, s) for a, s in answers if a in used_by]
    without = [(a, s) for a, s in answers if a not in used_by]
    ab: dict[str, Any] = {}
    for arm in ("lessons", "holdout"):
        sel = [a for a, _ in answers if arm_by.get(a) == arm]
        ab[arm] = _rate(sum(1 for a in sel if a in down_ids), len(sel))
    return {
        "all": _rate(len(down_ids), len(answers)),
        "with_lessons": _rate(sum(1 for a, _ in with_l if a in down_ids), len(with_l)),
        "without_lessons": _rate(sum(1 for a, _ in without if a in down_ids), len(without)),
        "ab": ab,
    }


def lessons_block(since: int) -> dict:
    with db.db() as conn:
        if not _table(conn, "lessons"):
            return {"available": False}
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM lessons GROUP BY status")}
        # Every active/archived lesson is evaluated (a new one whose few uses all
        # got 👎 must still surface); only the returned detail rows are capped.
        rows = conn.execute("SELECT id, text, status, use_count FROM lessons WHERE status IN ('active','archived') "
                            "ORDER BY use_count DESC, id").fetchall()
        down_shas = set()
        for r in conn.execute("SELECT m.content FROM feedback f JOIN messages m ON m.id = f.target_id "
                              "WHERE f.target_kind = 'brain_message' AND f.rating = -1 AND m.created_at >= ?", (since,)):
            down_shas.add(_sha(r[0]))
        top = []
        for r in rows:
            shas = {x[0] for x in conn.execute("SELECT DISTINCT answer_sha FROM lesson_uses WHERE lesson_id = ? "
                                               "AND created_at >= ?", (r["id"], since))}
            top.append({"id": r["id"], "text": r["text"][:120], "status": r["status"], "uses": r["use_count"],
                        "answers": len(shas), "down": len(shas & down_shas)})
    approved = counts.get("active", 0) + counts.get("archived", 0)
    return {"available": True, "counts": {k: counts.get(k, 0) for k in ("proposed", "active", "rejected", "archived")},
            "archived_pct": round(counts.get("archived", 0) * 100.0 / approved, 1) if approved else None,
            "top": top[:20], "_all": top}


def skills_block(now: int) -> dict:
    out: dict[str, Any] = {"with_without_delta": None, "with_without_note": "needs live evals (L2)"}
    with db.db() as conn:
        if _table(conn, "skill_state"):
            out["activations"] = int(conn.execute("SELECT COALESCE(SUM(activations), 0) FROM skill_state").fetchone()[0])
            out["top"] = [dict(r) for r in conn.execute(
                "SELECT key, activations, last_activated_at FROM skill_state WHERE activations > 0 "
                "ORDER BY activations DESC LIMIT 10")]
        out["indexed"] = int(conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]) if _table(conn, "skills") else 0
        if _table(conn, "skill_proposals"):
            props = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM skill_proposals GROUP BY status")}
            decided = props.get("approved", 0) + props.get("rejected", 0)
            out["proposals"] = props
            out["approval_pct"] = round(props.get("approved", 0) * 100.0 / decided, 1) if decided else None
            out["cost_eur"] = round(float(conn.execute(
                "SELECT COALESCE(SUM(cost_cents_est), 0) FROM skill_proposals").fetchone()[0]) / 100, 4)
    try:
        from ..skills import propose
        out["unused"] = len(propose.unused_skills(now))
        out["unused_days"] = propose.UNUSED_DAYS
    except Exception:
        out["unused"] = None
    return out


def sessions_block() -> dict:
    """First-attempt acceptance of session tasks (#18), before/after the first
    approved lesson. Absent tables → unavailable (sessions are another stack)."""
    with db.db() as conn:
        if not _table(conn, "session_tasks"):
            return {"available": False}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_tasks)")}
        if not {"status", "attempts", "created_at"} <= cols:
            return {"available": False}
        first = None
        if _table(conn, "lessons"):
            first = conn.execute("SELECT MIN(reviewed_at) FROM lessons WHERE status IN ('active','archived') "
                                 "AND reviewed_at IS NOT NULL").fetchone()[0]
        rows = [(r["attempts"], r["created_at"]) for r in conn.execute(
            "SELECT attempts, created_at FROM session_tasks WHERE status = 'done'")]
    return sessions_from_rows(rows, first)


def sessions_from_rows(rows: list[tuple[int | None, int]], first_lesson_at: int | None) -> dict:
    """``rows`` = (attempts, created_at) of done tasks."""
    def block(sel: list[tuple[int | None, int]]) -> dict:
        ok = sum(1 for a, _ in sel if (a or 0) <= 1)
        return {"done": len(sel), "first_try": ok,
                "first_try_pct": round(ok * 100.0 / len(sel), 1) if sel else None, "enough": len(sel) >= MIN_SAMPLE}
    out = {"available": True, "all": block(rows), "first_lesson_at": first_lesson_at}
    if first_lesson_at:
        out["before_lessons"] = block([r for r in rows if r[1] < first_lesson_at])
        out["after_lessons"] = block([r for r in rows if r[1] >= first_lesson_at])
    return out


def evals_block() -> dict:
    try:
        from . import evals_view
        ov = evals_view.overview()
    except Exception:
        return {"available": False}
    return {"available": ov["available"], "overall": ov["overall"],
            "suites": [{"suite": s["suite"], "baseline": s["baseline"]} for s in ov["suites"]]}


# ---------- report ----------

def _trend(cur: dict, prev: dict) -> str:
    if not cur["enough"] or not prev["enough"]:
        return "low_signal"
    d = (cur["per_100"] or 0) - (prev["per_100"] or 0)
    return "better" if d <= -TREND_POINTS else "worse" if d >= TREND_POINTS else "flat"


def _compare(a: dict, b: dict) -> str:
    """a = with lessons/skill arm, b = without."""
    if not a["enough"] or not b["enough"]:
        return "low_signal"
    d = (a["per_100"] or 0) - (b["per_100"] or 0)
    return "helps" if d <= -TREND_POINTS else "hurts" if d >= TREND_POINTS else "no_difference"


def report(days: int = DEFAULT_DAYS, now: int | None = None) -> dict:
    if not isinstance(days, int) or not 1 <= days <= 365:
        raise LearningError("bad_days", "days must be 1-365")
    now = now or int(time.time())
    since = now - days * 86400
    cur = downvotes(since, now + 1)
    prev = downvotes(since - days * 86400, since)
    lessons = lessons_block(since)
    skills = skills_block(now)
    signals = [{"code": "downvote_trend", "value": _trend(cur["all"], prev["all"]),
                "now": cur["all"]["per_100"], "before": prev["all"]["per_100"]},
               {"code": "lessons_effect", "value": _compare(cur["with_lessons"], cur["without_lessons"]),
                "with": cur["with_lessons"]["per_100"], "without": cur["without_lessons"]["per_100"]}]
    if ab_holdout_pct() > 0 or cur["ab"]["holdout"]["answers"]:
        signals.append({"code": "ab_effect", "value": _compare(cur["ab"]["lessons"], cur["ab"]["holdout"]),
                        "with": cur["ab"]["lessons"]["per_100"], "without": cur["ab"]["holdout"]["per_100"]})
    if lessons.get("available"):
        bad = [t for t in lessons.pop("_all") if t["status"] == "active" and t["down"] >= 2
               and t["down"] * 2 >= max(t["answers"], 1)]
        if bad:
            signals.append({"code": "lessons_to_review", "value": len(bad), "ids": [t["id"] for t in bad]})
    if skills.get("unused"):
        signals.append({"code": "unused_skills", "value": skills["unused"]})
    return {"days": days, "generated_at": now, "min_sample": MIN_SAMPLE, "settings": settings(),
            "downvotes": cur, "previous": prev, "lessons": lessons, "skills": skills,
            "sessions": sessions_block(), "evals": evals_block(), "signals": signals}


# ---------- scheduled curator (D14) ----------

def scheduled_maintenance(now: int | None = None) -> dict:
    """Daily system job: purge expired failure-log rows (D6) and, if enabled,
    let the curator *propose* archiving skills unused for 60 days (D14).
    Creates proposals only; never moves or deletes a skill."""
    from . import store
    out: dict[str, Any] = {"purged_failures": store.purge_expired(), "curator": "disabled", "proposed": []}
    if curator_enabled():
        from ..skills import propose
        made = propose.curator_scan(now)
        out["curator"] = "ran"
        out["proposed"] = [p["skill_name"] for p in made]
    return out
