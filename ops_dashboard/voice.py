"""Voice notes — local transcription using faster-whisper.

Endpoint flow:
  POST /api/voice/transcribe  (multipart file or raw audio bytes)
    → save to data/voice/<uuid>.webm
    → transcribe with faster-whisper (CPU, base model by default)
    → save transcript to voice_notes table
    → return { id, transcript, duration_sec, project_key }

Model selection (env var WHISPER_MODEL):
  - 'tiny'  (~75MB, fast, low quality)
  - 'base'  (~150MB, default, decent)
  - 'small' (~490MB, good)
  - 'medium' (~1.5GB, best quality, slower)

First call downloads the model into <data dir>/models/ (app_paths).

If faster-whisper is not installed, the endpoint returns 503 with a clear
hint to install it.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from . import app_paths as _app_paths

VOICE_DIR = _app_paths.path("voice", "data/voice")
VOICE_DIR.mkdir(parents=True, exist_ok=True)

_MODEL = None


def _model():
    """Lazy load faster-whisper model."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            "faster-whisper not installed. Run: pip install faster-whisper"
        )
    name = os.environ.get("WHISPER_MODEL", "base")
    cache = str(_app_paths.path("models", "data/models"))
    os.makedirs(cache, exist_ok=True)
    # device='cpu' is the safe default; 'auto' picks Metal on macOS
    device = os.environ.get("WHISPER_DEVICE", "cpu")
    compute = os.environ.get("WHISPER_COMPUTE", "int8")
    _MODEL = WhisperModel(name, device=device, compute_type=compute, download_root=cache)
    return _MODEL


def is_available() -> bool:
    try:
        import faster_whisper  # noqa
        return True
    except ImportError:
        return False


def save_audio(audio_bytes: bytes, suffix: str = ".webm") -> Path:
    """Save audio bytes to disk. Returns the path."""
    p = VOICE_DIR / f"{uuid.uuid4().hex}{suffix}"
    p.write_bytes(audio_bytes)
    return p


def transcribe_file(path: Path, language: str | None = None) -> dict:
    """Transcribe a single audio file. Returns dict with text + segments."""
    model = _model()
    t0 = time.perf_counter()
    segments, info = model.transcribe(
        str(path),
        language=language,
        beam_size=int(os.environ.get("WHISPER_BEAM", "1")),
        vad_filter=True,  # skip silence
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    duration_sec = getattr(info, "duration", None)
    elapsed = time.perf_counter() - t0
    return {
        "text": text,
        "language": getattr(info, "language", None),
        "duration_sec": duration_sec,
        "elapsed_sec": round(elapsed, 2),
    }


def transcribe_bytes(audio_bytes: bytes, suffix: str = ".webm",
                    project_key: str | None = None,
                    save_to_db: bool = True) -> dict:
    """High-level: save audio, transcribe, optionally save to DB."""
    path = save_audio(audio_bytes, suffix=suffix)
    try:
        result = transcribe_file(path)
    finally:
        # Don't delete audio by default; user can prune later
        pass
    note_id = None
    if save_to_db and result["text"]:
        from . import db as _db
        note_id = _db.add_voice_note(
            transcript=result["text"],
            audio_path=str(path),
            duration_sec=result.get("duration_sec"),
            project_key=project_key,
        )
    return {
        "id": note_id,
        "transcript": result["text"],
        "language": result.get("language"),
        "duration_sec": result.get("duration_sec"),
        "elapsed_sec": result.get("elapsed_sec"),
        "audio_path": str(path),
    }
