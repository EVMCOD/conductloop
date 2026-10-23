"""Narration: a spoken track under a clip.

The H3 family renders its own sound track but is the model this account
cannot pay for; Hailuo is silent and cheap. This is the path to a clip with
a voice: the script is spoken, the audio is mixed under the picture, and the
video stream is copied untouched.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ops_dashboard import db_media, media


@pytest.fixture
def vault(tmp_path, monkeypatch):
    from ops_dashboard import db
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(media.app_paths, "data_dir", lambda: tmp_path)
    db.init_db()
    media.set_engine(_Engine())
    yield tmp_path
    media.set_engine(None)


class _Engine(media.MediaEngine):
    """A clip and a voice, without touching the network."""

    name = "engine"

    def available(self):
        return True

    def synthesize_speech(self, text, **kw):
        path = media.media_dir(kw.get("project_key")) / "voice.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        # A real, decodable mp3 so ffprobe and ffmpeg have something to work on.
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=2", path],
                       capture_output=True, timeout=60)
        return media.MediaResult(ok=True, kind="audio", path=str(path),
                                 meta={"voice_id": kw.get("voice_id")})


def _silent_video(tmp_path: Path) -> str:
    out = tmp_path / "media" / "p" / "clip.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=navy:s=320x180:d=3", "-pix_fmt", "yuv420p", out],
                   capture_output=True, timeout=60)
    return str(out)


# ── the mix itself ─────────────────────────────────────────────────────────

def test_the_mix_puts_audio_under_the_picture(vault):
    tmp_path = vault
    if not media.ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    video = _silent_video(tmp_path)
    voice = tmp_path / "media" / "p" / "voice.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=2", voice],
                   capture_output=True, timeout=60)
    out = tmp_path / "media" / "p" / "narrated.mp4"
    media.mix_voiceover(video, str(voice), str(out))
    assert out.exists()
    info = media.probe(out)
    assert info["has_audio"] is True, "the narration must be in the result"
    assert info["width"] == 320


def test_the_mix_refuses_a_file_outside_the_library(vault):
    tmp_path = vault
    with pytest.raises(media.MediaError, match="media library"):
        media.mix_voiceover("/etc/hosts", "/etc/hosts", str(tmp_path / "out.mp4"))


def test_the_mix_needs_both_a_video_and_a_voice(vault):
    tmp_path = vault
    video = _silent_video(tmp_path)
    with pytest.raises(media.MediaError, match="media library"):
        media.mix_voiceover(video, "/etc/hosts", str(tmp_path / "out.mp4"))


# ── the product operation ──────────────────────────────────────────────────

def test_a_narrated_clip_is_recorded_as_an_asset(vault):
    if not media.ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    video = _silent_video(vault)
    res = media.voiceover(video, "Hola, esto es una prueba de narración.",
                          project_key="conductloop")
    assert res.ok
    assert res.path and os.path.isfile(res.path)
    info = media.probe(res.path)
    assert info["has_audio"] is True
    rows = db_media.list_assets(project_key="conductloop", kind="video")
    assert rows, "the narrated clip must be in the library"
    assert rows[0]["meta"].get("voiceover") is True


def test_a_narrated_clip_remembers_its_voice(vault):
    if not media.ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    video = _silent_video(vault)
    media.voiceover(video, "Texto.", project_key="p",
                    voice_id="Spanish_Narrator", emotion="neutral", speed=1.0)
    row = db_media.list_assets(project_key="p", kind="video")[0]
    assert row["meta"]["voice_id"] == "Spanish_Narrator"
    assert row["meta"]["emotion"] == "neutral"


def test_an_empty_script_is_refused(vault):
    with pytest.raises(media.MediaError, match="script is required"):
        media.voiceover(_silent_video(vault), "   ")


def test_a_video_outside_the_library_is_refused(vault):
    with pytest.raises(media.MediaError, match="media library"):
        media.voiceover("/etc/hosts", "Hola.")


# ── the endpoint ───────────────────────────────────────────────────────────

def test_a_library_clip_can_be_narrated_end_to_end(vault):
    """The path the endpoint takes: a stored asset in, a narrated asset out."""
    if not media.ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    video = _silent_video(vault)
    asset_id = db_media.add_asset(kind="video", prompt="clip", path=video, status="ready")
    assert asset_id
    res = media.voiceover(video, "Texto de prueba.", project_key="p")
    assert res.ok
    assert res.meta["asset_id"]
    assert db_media.get_asset(res.meta["asset_id"]) is not None
