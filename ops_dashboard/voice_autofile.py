"""Voice → auto-file pipeline.

Takes a voice-note transcript (from the existing Whisper transcription) and
files it where it belongs — a durable memory, a decision, the quick-capture
inbox, or a plain voice note — based on how you started talking. Say "remember
that…", "we decided…", "add to inbox…", or just talk.

    from . import voice_autofile
    voice_autofile.file_transcript("remember that the API key lives in the keychain")
"""
from __future__ import annotations

import hashlib
import logging

from . import db

log = logging.getLogger("conductloop.voice_autofile")


def classify(transcript: str) -> str:
    """memory | decision | capture | note — from the opening intent."""
    t = (transcript or "").strip().lower()
    if t.startswith(("remember", "note to self", "don't forget")) or "remember that" in t:
        return "memory"
    if t.startswith(("we decided", "i decided", "decision:")) or "we decided" in t or "the decision is" in t:
        return "decision"
    if t.startswith(("add to inbox", "capture", "todo", "to do", "remind me")):
        return "capture"
    return "note"


def file_transcript(transcript: str, *, kind: str | None = None,
                    project_key: str | None = None) -> dict:
    """Route a transcript to the right store. Returns {kind, id|key}."""
    transcript = (transcript or "").strip()
    if not transcript:
        raise ValueError("empty transcript")
    kind = kind or classify(transcript)

    if kind == "memory":
        key = f"voice:{hashlib.sha1(transcript.encode()).hexdigest()[:8]}"
        db.memory_set(key, transcript)
        return {"kind": "memory", "key": key}
    if kind == "decision":
        title = transcript[:60]
        did = db.add_decision(title=title, decision=transcript, project_key=project_key)
        return {"kind": "decision", "id": did}
    if kind == "capture":
        from . import capture
        cid = capture.add(transcript, kind="task", source="voice")
        return {"kind": "capture", "id": cid}
    vid = db.add_voice_note(transcript, project_key=project_key)
    return {"kind": "note", "id": vid}
