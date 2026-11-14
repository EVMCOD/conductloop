"""Image-to-video: the keyframe path, the per-model rules and the API.

The connector contract behind these tests was read off the live
``submit_video_generation`` schema and checked against real renders
(2026-10-23). The rules are not uniform across models, and each one below
exists because getting it wrong costs money: the engine either rejects the
job or, worse, accepts it and renders something you did not ask for.

No test calls a generation API. The CLI is stubbed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ops_dashboard import media

# The CI installs ffmpeg and sets CONDUCTLOOP_REQUIRE_FFMPEG=1: there the
# mixing has to be proven with the real tool, so a missing ffmpeg is a
# failure and not a skip. Everywhere else the test is skipped.
needs_ffmpeg = pytest.mark.skipif(
    not (media.ffmpeg_available() or os.environ.get("CONDUCTLOOP_REQUIRE_FFMPEG")),
    reason="ffmpeg not installed")


# ── a recording engine, so we can assert on what would be sent ─────────────

class _RecordingEngine(media.MediaEngine):
    """Records submit arguments; never touches the network."""

    name = "recording"

    def __init__(self, task_id: str = "task-1"):
        self.calls: list[dict] = []
        self.uploads: list[str] = []
        self.task_id = task_id

    def available(self) -> bool:
        return True

    def upload(self, path, *, mime_type=None):
        self.uploads.append(str(path))
        return "https://engine.invalid/tmp/keyframe.png"

    def submit_video(self, prompt, **kw):
        self.calls.append(dict(kw, prompt=prompt))
        return media.MediaResult(
            ok=True, kind="video", task_id=self.task_id, status="queued",
            meta={"model": kw.get("model")},
        )


@pytest.fixture(autouse=True)
def _no_engine_leaks():
    """No test may leave a stub engine behind for the next one."""
    yield
    media.set_engine(None)


def _keyframe(tmp_path: Path) -> Path:
    p = tmp_path / "keyframe.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nstub")
    return p


# ── the per-model rules ────────────────────────────────────────────────────

def test_hailuo_only_takes_its_two_durations():
    spec = media.VIDEO_SPECS["MiniMax-Hailuo-2.3"]
    assert spec.accepts_duration(6)
    assert spec.accepts_duration(10)
    # 8 s is inside the 6-10 range but not a duration the engine accepts.
    assert not spec.accepts_duration(8)


def test_hailuo_1080p_is_only_valid_at_six_seconds():
    spec = media.VIDEO_SPECS["MiniMax-Hailuo-2.3"]
    assert "1080P" in spec.resolutions_for(6)
    assert "1080P" not in spec.resolutions_for(10)


def test_hailuo_takes_no_ratio_but_h3_does():
    assert media.VIDEO_SPECS["MiniMax-Hailuo-2.3"].accepts_ratio is False
    assert media.VIDEO_SPECS["MiniMax-H3"].accepts_ratio is True


def test_h3_max_caps_at_768p():
    assert "2K" not in media.VIDEO_SPECS["MiniMax-H3-Max"].resolutions


def test_only_the_h3_family_renders_sound():
    sounding = {n for n, s in media.VIDEO_SPECS.items() if s.native_audio}
    assert sounding == {"MiniMax-H3", "MiniMax-H3-Max"}


def test_hailuo_has_no_last_frame_mode():
    assert "last_frame" not in media.VIDEO_SPECS["MiniMax-Hailuo-2.3"].keyframe


# ── validation happens before anything is paid for ─────────────────────────

def test_invalid_duration_is_refused_before_the_engine_is_called(tmp_path):
    eng = _RecordingEngine()
    media.set_engine(eng)
    with pytest.raises(media.MediaError, match="MiniMax-Hailuo-2.3 takes"):
        media.submit_video("a shot", model="MiniMax-Hailuo-2.3", duration=8)
    assert eng.calls == []


def test_invalid_resolution_for_that_duration_is_refused(tmp_path):
    eng = _RecordingEngine()
    media.set_engine(eng)
    with pytest.raises(media.MediaError, match="1080P"):
        media.MatrixEngine.submit_video(
            media.MatrixEngine.__new__(media.MatrixEngine),
            "a shot", model="MiniMax-Hailuo-2.3", duration=10, resolution="1080P")
    assert eng.calls == []


def test_a_keyframe_the_model_cannot_use_is_refused(tmp_path):
    """Hailuo has no last_frame mode; asking for one must not reach the API."""
    with pytest.raises(media.MediaError, match="last frame"):
        media.MatrixEngine.submit_video(
            media.MatrixEngine.__new__(media.MatrixEngine),
            "a shot", model="MiniMax-Hailuo-2.3", duration=6,
            last_frame_path=str(_keyframe(tmp_path)))


def test_a_missing_keyframe_is_refused(tmp_path):
    media.set_engine(_RecordingEngine())
    with pytest.raises(media.MediaError, match="keyframe not found"):
        media.submit_video("a shot", model="MiniMax-H3", duration=6,
                           image_path=str(tmp_path / "nope.png"))


def test_an_unsupported_keyframe_format_is_refused(tmp_path):
    gif = tmp_path / "frame.gif"
    gif.write_bytes(b"GIF89a")
    media.set_engine(_RecordingEngine())
    with pytest.raises(media.MediaError, match="unsupported keyframe format"):
        media.submit_video("a shot", model="MiniMax-H3", duration=6, image_path=str(gif))


# ── the keyframe path itself ───────────────────────────────────────────────

def test_image_path_is_uploaded_and_sent_as_the_first_frame(tmp_path):
    """The whole image-to-video path: local file -> engine url -> input_image."""
    calls: list[dict] = []

    class _E(media.MatrixEngine):
        def __init__(self):
            self._cli = "/bin/true"

        def upload(self, path, *, mime_type=None):
            calls.append({"upload": str(path)})
            return "https://engine.invalid/tmp/kf.png"

        def _call(self, tool, args, **kw):
            calls.append({"tool": tool, "args": args})
            return {"code": 0, "task_id": "task-9", "status": "submitted"}

    res = _E().submit_video("slow push in", model="MiniMax-H3", duration=6,
                            image_path=str(_keyframe(tmp_path)))

    assert res.task_id == "task-9"
    submit = next(c for c in calls if c.get("tool", "").endswith("submit_video_generation"))
    assert submit["args"]["input_image"]["url"] == "https://engine.invalid/tmp/kf.png"
    assert submit["args"]["input_image"]["mime_type"] == "image/png"
    assert submit["args"]["reference_type"] == "first_frame"
    # The keyframe was uploaded from disk, not passed as a path.
    assert any("upload" in c for c in calls)


def test_keyframe_mode_uses_adaptive_because_the_image_sets_the_aspect(tmp_path):
    """Sending a ratio next to a keyframe is wrong: the engine follows the image."""
    args: list[dict] = []

    class _E(media.MatrixEngine):
        def __init__(self):
            self._cli = "/bin/true"

        def upload(self, path, *, mime_type=None):
            return "https://engine.invalid/tmp/kf.png"

        def _call(self, tool, a, **kw):
            args.append(a)
            return {"code": 0, "task_id": "t"}

    _E().submit_video("push in", model="MiniMax-H3", duration=6, ratio="9:16",
                      image_path=str(_keyframe(tmp_path)))
    assert args[0]["ratio"] == "adaptive"

    # Text-to-video, same model, still gets the ratio it asked for.
    _E().submit_video("push in", model="MiniMax-H3", duration=6, ratio="9:16")
    assert args[1]["ratio"] == "9:16"


def test_hailuo_submission_carries_no_ratio_at_all(tmp_path):
    args: list[dict] = []

    class _E(media.MatrixEngine):
        def __init__(self):
            self._cli = "/bin/true"

        def upload(self, path, *, mime_type=None):
            return "https://engine.invalid/tmp/kf.png"

        def _call(self, tool, a, **kw):
            args.append(a)
            return {"code": 0, "task_id": "t"}

    _E().submit_video("push in", model="MiniMax-Hailuo-2.3", duration=6,
                      image_path=str(_keyframe(tmp_path)))
    assert "ratio" not in args[0]


def test_the_upload_resolves_relative_paths(tmp_path, monkeypatch):
    """The CLI runs in a temp cwd, so a relative keyframe path must be resolved.

    Found by running it: a relative path reached the CLI as a path relative
    to the temp directory and the upload failed with "file does not exist".
    """
    seen: list[str] = []

    class _E(media.MatrixEngine):
        def __init__(self):
            self._cli = "/bin/true"

        def _run(self, args, **kw):
            seen.append(args[1])
            return json.dumps({"temp_url": "https://engine.invalid/x.png"})

    monkeypatch.chdir(tmp_path)
    _keyframe(tmp_path)
    _E().upload("keyframe.png")
    assert os.path.isabs(seen[0])
    assert Path(seen[0]) == (tmp_path / "keyframe.png").resolve()


def test_upload_refuses_an_unsupported_format(tmp_path):
    class _E(media.MatrixEngine):
        def __init__(self):
            self._cli = "/bin/true"

    gif = tmp_path / "a.gif"
    gif.write_bytes(b"GIF89a")
    with pytest.raises(media.MediaError, match="unsupported keyframe format"):
        _E().upload(str(gif))


def test_upload_refuses_an_oversized_keyframe(tmp_path):
    class _E(media.MatrixEngine):
        def __init__(self):
            self._cli = "/bin/true"

    big = tmp_path / "big.png"
    with open(big, "wb") as fh:
        fh.truncate(media.KEYFRAME_MAX_BYTES + 1)
    with pytest.raises(media.MediaError, match="up to 30 MB"):
        _E().upload(str(big))


# ── engine errors used to look like successes ──────────────────────────────

def test_an_error_payload_is_raised_not_returned(monkeypatch):
    """{"code": 402} came back with exit status 0 and read as a good submit."""
    eng = media.MatrixEngine.__new__(media.MatrixEngine)
    eng._cli = "/bin/true"
    monkeypatch.setattr(eng, "_run", lambda *a, **k: json.dumps(
        {"code": 402, "message": "the account cannot cover this model"}))
    with pytest.raises(media.MediaError) as excinfo:
        eng._call(media.VIDEO_SUBMIT_TOOL, {"prompt": "x"})
    assert excinfo.value.code == 402
    assert "cannot cover" in str(excinfo.value)


def test_a_zero_code_is_still_a_success(monkeypatch):
    eng = media.MatrixEngine.__new__(media.MatrixEngine)
    eng._cli = "/bin/true"
    monkeypatch.setattr(eng, "_run", lambda *a, **k: json.dumps({"code": 0, "task_id": "t"}))
    assert eng._call(media.VIDEO_SUBMIT_TOOL, {})["task_id"] == "t"


# ── falling back when the wallet says no ───────────────────────────────────

def _engine_failing_then_ok(failing: list[str]):
    class _E(media.MatrixEngine):
        def __init__(self):
            self._cli = "/bin/true"

        def upload(self, path, *, mime_type=None):
            return "https://engine.invalid/tmp/kf.png"

        def _call(self, tool, args, **kw):
            if failing:
                raise MediaErrorRefused(failing.pop(0))
            return {"code": 0, "task_id": "task-fb"}

    return _E()


class MediaErrorRefused(media.MediaError):
    def __init__(self, message):
        super().__init__(message, code=402)


def test_a_refused_render_is_retried_on_the_model_the_plan_can_pay_for(tmp_path):
    eng = _engine_failing_then_ok(["no credits for H3"])
    res = eng.submit_video("a shot", model="MiniMax-H3", duration=6,
                           resolution="768P", image_path=str(_keyframe(tmp_path)))
    assert res.task_id == "task-fb"
    assert res.meta["model"] == "MiniMax-Hailuo-2.3"
    assert res.meta["requested_model"] == "MiniMax-H3"
    assert res.meta["fell_back"] is True


def test_the_fallback_does_not_claim_the_sound_track_it_lost(tmp_path):
    """Hailuo is silent. Reporting H3's native audio after a fallback is a lie."""
    eng = _engine_failing_then_ok(["no credits for H3"])
    res = eng.submit_video("a shot", model="MiniMax-H3", duration=6,
                           resolution="768P", image_path=str(_keyframe(tmp_path)))
    assert res.meta["native_audio"] is False


def test_the_fallback_asks_for_a_resolution_the_new_model_accepts(tmp_path):
    seen: list[dict] = []

    class _E(media.MatrixEngine):
        def __init__(self):
            self._cli = "/bin/true"

        def upload(self, path, *, mime_type=None):
            return "https://engine.invalid/tmp/kf.png"

        def _call(self, tool, args, **kw):
            seen.append(dict(args))
            if len(seen) == 1:
                raise MediaErrorRefused("no credits")
            return {"code": 0, "task_id": "t"}

    _E().submit_video("a shot", model="MiniMax-H3", duration=6,
                      resolution="2K", image_path=str(_keyframe(tmp_path)))
    assert seen[1]["model"] == "MiniMax-Hailuo-2.3"
    assert seen[1]["resolution"] == "768P"


def test_a_non_credit_error_is_not_retried_on_another_model(tmp_path):
    """Only the wallet is worth falling back for; a bad prompt is not."""

    class _E(media.MatrixEngine):
        def __init__(self):
            self._cli = "/bin/true"

        def upload(self, path, *, mime_type=None):
            return "https://engine.invalid/tmp/kf.png"

        def _call(self, tool, args, **kw):
            raise media.MediaError("prompt rejected by the safety filter")

    with pytest.raises(media.MediaError, match="prompt rejected"):
        _E().submit_video("a shot", model="MiniMax-H3", duration=6,
                          image_path=str(_keyframe(tmp_path)))


# ── one call animates a generated image ────────────────────────────────────

def test_animate_image_picks_a_duration_the_model_actually_takes(tmp_path, monkeypatch):
    seen: dict = {}

    def _submit(prompt, **kw):
        seen.update(kw)
        return {"job_id": 1, "task_id": "t", "status": "queued"}

    monkeypatch.setattr(media, "submit_video", _submit)
    # The product asks a provider for the work; the stub is what answers.
    media.set_engine(_RecordingEngine())
    media.animate_image(str(_keyframe(tmp_path)), "breathe")
    # 6 is the first duration Hailuo really accepts; 8 would be rejected.
    assert seen["duration"] == 6
    assert seen["reference_type"] == "first_frame"
    assert seen["image_path"]


def test_animate_image_keeps_a_valid_requested_duration(tmp_path, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(media, "submit_video", lambda p, **kw: seen.update(kw) or {})
    media.set_engine(_RecordingEngine())
    media.animate_image(str(_keyframe(tmp_path)), "breathe", duration=10)
    assert seen["duration"] == 10


# ── what the UI is allowed to offer ────────────────────────────────────────

def test_capabilities_describe_every_model_for_the_form():
    caps = {c["model"]: c for c in media.video_capabilities()}
    assert set(caps) == set(media.VIDEO_MODELS)
    assert caps["MiniMax-H3"]["default"] is True
    assert caps["MiniMax-H3-Max"]["fast"] is True
    assert caps["MiniMax-Hailuo-2.3"]["resolutions_by_duration"]["6"] == ["768P", "1080P"]
    assert caps["MiniMax-H3"]["native_audio"] is True


def test_status_exposes_the_capabilities_and_keyframe_rules():
    st = media.engine_status()
    assert st["keyframe_formats"] == sorted(media.KEYFRAME_MIME)
    assert st["video_capabilities"]
    assert st["fallback_video_model"] == media.FALLBACK_VIDEO_MODEL


# ── a re-poll must not overwrite the finished file ─────────────────────────

def test_polling_a_finished_task_twice_keeps_both_files(tmp_path, monkeypatch):
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    counter = {"n": 0}

    eng = media.MatrixEngine.__new__(media.MatrixEngine)
    eng._cli = "/bin/true"
    monkeypatch.setattr(eng, "_call", lambda *a, **k: {
        "code": 0, "status": "succeeded", "video_url": "https://engine.invalid/v.mp4"})

    def _dl(url, dest):
        counter["n"] += 1
        dest = Path(dest)
        dest.write_bytes(b"x")
        return first if counter["n"] == 1 else second

    monkeypatch.setattr(eng, "_download", _dl)
    monkeypatch.setattr(media, "media_dir", lambda pk=None: tmp_path)

    a = eng.poll_video("task-7", model="MiniMax-H3")
    b = eng.poll_video("task-7", model="MiniMax-H3")
    assert a.path != b.path


# ── path containment ───────────────────────────────────────────────────────

def test_allowed_media_path_keeps_files_inside_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(media.app_paths, "data_dir", lambda: tmp_path)
    inside = tmp_path / "media" / "p" / "a.png"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x")
    assert media.allowed_media_path(str(inside)) == str(inside.resolve())
    # Outside the data dir: no, even though it exists.
    assert media.allowed_media_path("/etc/hosts") is None


def test_allowed_media_path_reports_a_missing_file_as_none(tmp_path, monkeypatch):
    monkeypatch.setattr(media.app_paths, "data_dir", lambda: tmp_path)
    assert media.allowed_media_path(str(tmp_path / "media" / "gone.png")) is None


def test_a_sibling_prefix_directory_is_not_inside(tmp_path, monkeypatch):
    """/data/media-evil must not pass a startswith check against /data/media."""
    data = tmp_path / "data"
    data.mkdir()
    evil = tmp_path / "data" / "media-evil"
    evil.mkdir()
    (evil / "a.png").write_bytes(b"x")
    monkeypatch.setattr(media.app_paths, "data_dir", lambda: data)
    assert media.allowed_media_path(str(evil / "a.png")) is None


def test_a_keyframe_cannot_be_taken_from_outside_the_media_library(tmp_path, monkeypatch):
    """The data directory also holds the database; it must not leave the Mac."""
    data = tmp_path / "data"
    data.mkdir()
    db = data / "ops.db"
    db.write_bytes(b"sqlite payload")
    monkeypatch.setattr(media.app_paths, "data_dir", lambda: data)
    # Local work (ffmpeg) may still read it...
    assert media.allowed_media_path(str(db), scope="data") == str(db.resolve())
    # ...but nothing that leaves this Mac may.
    assert media.allowed_media_path(str(db), scope="media") is None


# ── probing reports whether there is actually sound ────────────────────────

@needs_ffmpeg
def test_probe_reports_the_audio_track(tmp_path):
    silent = tmp_path / "s.mp4"
    with_audio = tmp_path / "a.mp4"
    subprocess_ok = os.system(
        f"ffmpeg -v error -y -f lavfi -i color=c=blue:s=64x64:d=1 -pix_fmt yuv420p {silent} >/dev/null 2>&1")
    os.system(f"ffmpeg -v error -y -f lavfi -i color=c=blue:s=64x64:d=1 "
              f"-f lavfi -i sine=frequency=440:duration=1 -c:a aac -pix_fmt yuv420p "
              f"-shortest {with_audio} >/dev/null 2>&1")
    if subprocess_ok != 0:
        pytest.skip("ffmpeg could not synthesise a test clip")
    assert media.probe(silent)["has_audio"] is False
    assert media.probe(with_audio)["has_audio"] is True
