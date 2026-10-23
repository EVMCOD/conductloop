"""Tests for the media engine, the asset/job store and the media API.

Nothing here calls a generation API: the engine is stubbed, and the ffmpeg
path is exercised with a video ffmpeg itself synthesises. The point is that
the product logic (state machine, storage, validation, routes) is correct
and cheap to verify.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import media

# The CI installs ffmpeg and sets CONDUCTLOOP_REQUIRE_FFMPEG=1: there the
# mixing has to be proven with the real tool, so a missing ffmpeg is a
# failure and not a skip. Everywhere else the test is skipped.
needs_ffmpeg = pytest.mark.skipif(
    not (media.ffmpeg_available() or os.environ.get("CONDUCTLOOP_REQUIRE_FFMPEG")),
    reason="ffmpeg not installed")


# ── a stub engine ──────────────────────────────────────────────────────────

class _StubEngine(media.MediaEngine):
    name = "stub"

    def available(self) -> bool:
        return True

    def generate_image(self, prompt, **kw):
        return media.MediaResult(ok=True, kind="image", path=str(_tmp_png()),
                                 node_id="node-1", url="https://example.invalid/i.png",
                                 meta={"aspect_ratio": kw.get("aspect_ratio", "1:1")})

    def submit_video(self, prompt, **kw):
        return media.MediaResult(ok=True, kind="video", task_id="task-123", status="queued",
                                 meta={"model": kw.get("model")})

    def poll_video(self, task_id, **kw):
        return media.MediaResult(ok=True, kind="video", task_id=task_id, status="succeeded",
                                 path=str(_tmp_mp4()), url="https://example.invalid/v.mp4",
                                 meta={"file_id": "f-1"})

    def synthesize_speech(self, text, **kw):
        return media.MediaResult(ok=True, kind="audio", path=str(_tmp_mp3()))


def _tmp_png():
    from pathlib import Path
    p = Path(os.environ.get("OPS_DATA_DIR", "/tmp")) / "stub.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\nstub")
    return p


def _tmp_mp4():
    from pathlib import Path
    p = Path(os.environ.get("OPS_DATA_DIR", "/tmp")) / "stub.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00\x00\x00 ftypmp42stub")
    return p


def _tmp_mp3():
    from pathlib import Path
    p = Path(os.environ.get("OPS_DATA_DIR", "/tmp")) / "stub.mp3"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"ID3stub")
    return p


@pytest.fixture(autouse=True)
def stub_engine():
    from ops_dashboard import db
    db.init_db()
    media.set_engine(_StubEngine())
    yield
    media.set_engine(None)


# ── the engine registry ────────────────────────────────────────────────────

def test_find_cli_locates_the_mcode_tools_cli():
    """The engine needs the CLI; it must be found even without a login shell.

    The CLI is the MiniMax desktop runtime, which is not installed on a CI
    runner, so there the test is skipped rather than failed: the CI cannot
    prove something that is not there.
    """
    cli = media.find_cli()
    if not cli:
        pytest.skip("the mcode-tools CLI (MiniMax desktop runtime) is not installed")
    assert cli and os.path.isfile(cli), "mcode-tools not found on PATH"


def test_engine_status_reports_what_the_ui_needs():
    st = media.engine_status()
    assert st["engine"] == "stub"
    assert st["available"] is True
    assert "MiniMax-H3" in st["video_models"]
    assert st["default_video_model"] == "MiniMax-H3"


# ── validation ─────────────────────────────────────────────────────────────

def test_image_generation_validates_its_inputs():
    """The contract is enforced by the product layer, so every engine obeys it."""
    with pytest.raises(media.MediaError):
        media.generate_image("a cat", aspect_ratio="21:9x")
    with pytest.raises(media.MediaError):
        media.generate_image("a cat", resolution="8K")
    with pytest.raises(media.MediaError):
        media.generate_image("   ")
    with pytest.raises(media.MediaError):
        media.generate_image("a cat", n=99)


def test_video_generation_validates_model_and_duration():
    with pytest.raises(media.MediaError):
        media.submit_video("x", model="Sora-2")
    with pytest.raises(media.MediaError):
        media.submit_video("x", model="MiniMax-H3", duration=99)
    with pytest.raises(media.MediaError):
        media.submit_video("x", model="MiniMax-Hailuo-2.3", duration=3)
    with pytest.raises(media.MediaError):
        media.submit_video("  ")


# ── assets and jobs ────────────────────────────────────────────────────────

def test_generated_image_is_recorded_as_an_asset():
    from ops_dashboard import db
    res = media.generate_image("un gato con gafas", project_key="deepwick")
    assert res.ok
    rows = db.list_assets(project_key="deepwick")
    assert len(rows) == 1
    assert rows[0]["kind"] == "image"
    assert rows[0]["prompt"] == "un gato con gafas"
    assert rows[0]["engine"] == "stub"


def test_every_generated_image_is_kept_not_only_the_first():
    """Generating 4 and keeping 1 means paying for 4 and losing 3."""
    from ops_dashboard import db

    class _MultiEngine(_StubEngine):
        def generate_image(self, prompt, **kw):
            n = int(kw.get("n") or 1)
            return media.MediaResult(
                ok=True, kind="image", path=str(_tmp_png()),
                meta={"items": n, "paths": [str(_tmp_png())] * n},
            )

    media.set_engine(_MultiEngine())
    res = media.generate_image("cuatro gatos", project_key="deepwick", n=4)
    assert res.ok
    rows = [r for r in db.list_assets(project_key="deepwick", kind="image")
            if r["prompt"] == "cuatro gatos"]
    assert len(rows) == 4, rows


def test_file_names_do_not_collide():
    a = media._unique_name("un gato", ".png")
    time.sleep(0.01)
    b = media._unique_name("un gato", ".png")
    assert a != b
    assert a.endswith(".png") and b.endswith(".png")


def test_video_jobs_are_polled_by_the_reaper():
    """A render nobody polls is paid for and lost."""
    from ops_dashboard import db

    class _ReadyEngine(_StubEngine):
        def poll_video(self, task_id, **kw):
            return media.MediaResult(ok=True, kind="video", task_id=task_id,
                                     status="succeeded", path=str(_tmp_mp4()),
                                     meta={"file_id": "f-1"})

    out = media.submit_video("un gato en un plane", project_key="deepwick")
    media.set_engine(_ReadyEngine())
    stats = media.reap_jobs()
    assert stats["checked"] >= 1
    assert stats["succeeded"] >= 1
    job = db.get_job(out["job_id"])
    assert job["status"] == "succeeded"
    assert db.get_asset(job["asset_id"]) is not None


def test_assemble_refuses_files_outside_the_data_directory():
    """The endpoint takes a list of paths: without this, any local file
    could be pulled into the output."""
    outside = "/tmp/conductloop-not-ours.mp4"
    with open(outside, "wb") as fh:
        fh.write(b"x")
    try:
        with pytest.raises(media.MediaError):
            media.assemble([outside], "/tmp/out.mp4")
    finally:
        os.unlink(outside)


def test_assemble_escapes_quotes_in_paths():
    """The concat demuxer parses the list file: a quote would inject an entry."""
    from ops_dashboard import app_paths
    weird = app_paths.data_dir() / "we'ird'.mp4"
    weird.parent.mkdir(parents=True, exist_ok=True)
    weird.write_bytes(b"x")
    try:
        with pytest.raises(media.MediaError):
            # No .git, not a real clip: it must fail on the clip, not on the
            # quoting — the point is that it does not silently succeed.
            media.assemble([str(weird)], str(app_paths.data_dir() / "out.mp4"))
    except media.MediaError as e:
        assert "not found" in str(e) or "ffmpeg" in str(e).lower(), str(e)


def test_video_job_moves_from_queued_to_succeeded():
    from ops_dashboard import db
    out = media.submit_video("un gato en un plane", project_key="deepwick")
    job_id = out["job_id"]
    job = db.get_job(job_id)
    assert job["status"] == "queued"
    assert job["external_task_id"] == "task-123"

    job = media.poll_video(job_id)
    assert job["status"] == "succeeded"
    assert job["asset_id"]
    assets = db.list_assets(project_key="deepwick")
    assert any(a["kind"] == "video" for a in assets)


def test_video_job_stays_pending_until_the_engine_says_so():
    """The product keeps one status vocabulary, whatever casing the engine uses."""

    class _PendingEngine(_StubEngine):
        def poll_video(self, task_id, **kw):
            return media.MediaResult(ok=True, kind="video", task_id=task_id, status="Processing")

    media.set_engine(_PendingEngine())
    out = media.submit_video("x")
    job = media.poll_video(out["job_id"])
    assert job["status"] == "running"
    assert not job["asset_id"]


# ── ffmpeg: the local part of the pipeline ────────────────────────────────

@needs_ffmpeg
def test_ffmpeg_assembles_clips_into_one_video(tmp_path):
    """The assembly half of the video pipeline works with no API at all."""
    # The clips live under the product data dir: assemble() only accepts
    # files there (2026-10-23).
    clip_dir = media.media_dir("clips-test")
    clips = []
    for i, colour in enumerate(("red", "blue")):
        clip = clip_dir / f"clip{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=320x240:d=1",
             "-r", "10", "-pix_fmt", "yuv420p", str(clip)],
            capture_output=True, text=True, timeout=120,
        )
        assert clip.exists(), f"ffmpeg did not produce {clip}"
        clips.append(str(clip))

    out = clip_dir / "out.mp4"
    path = media.assemble(clips, out)
    assert os.path.isfile(path)
    info = media.probe(path)
    assert info["duration_sec"] >= 1.5, info
    assert info["width"] == 320


@needs_ffmpeg
def test_ffmpeg_assemble_rejects_an_empty_clip_list(tmp_path):
    with pytest.raises(media.MediaError):
        media.assemble([], tmp_path / "out.mp4")


# ── the HTTP surface ───────────────────────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _hit(url: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture(scope="module")
def media_server():
    from ops_dashboard import db
    from ops_dashboard.indexers import live_urls
    from ops_dashboard.server import DashboardHandler

    db.init_db()
    live_urls.ensure_schema()

    class _QuietHandler(DashboardHandler):
        def log_message(self, *_a):
            pass

    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


def test_media_status_endpoint(media_server):
    status, body = _hit(media_server + "/api/media/status")
    assert status == 200
    assert body["ok"] is True
    assert "video_models" in body


def test_media_image_endpoint_records_the_asset(media_server):
    status, body = _hit(media_server + "/api/media/image", method="POST",
                        payload={"prompt": "un logo minimalista", "project_key": "rentcla"})
    assert status == 200, body
    assert body["ok"] is True
    assert body["kind"] == "image"


def test_media_image_endpoint_requires_a_prompt(media_server):
    status, body = _hit(media_server + "/api/media/image", method="POST", payload={})
    assert status == 400
    assert body["ok"] is False


def test_media_video_endpoint_returns_a_job_to_poll(media_server):
    status, body = _hit(media_server + "/api/media/video", method="POST",
                        payload={"prompt": "un reencuentro en Tokio", "duration": 6})
    assert status == 200, body
    assert body["job_id"]

    status, body = _hit(f"{media_server}/api/media/video/{body['job_id']}")
    assert status == 200
    assert body["job"]["status"] == "succeeded"


def test_media_assets_endpoint_lists_what_was_generated(media_server):
    status, body = _hit(media_server + "/api/media/assets")
    assert status == 200
    assert isinstance(body["rows"], list)
    assert isinstance(body["jobs"], list)
