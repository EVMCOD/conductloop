"""The media HTTP surface: what the client can and cannot ask for.

The image-to-video endpoints take an asset id from the client, so the
interesting failures are in the plumbing: a numeric id that reads as absent,
a path that points outside the media library, a combination the engine would
charge for and then reject.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from ops_dashboard import db_media, media
from ops_dashboard import routes_media  # noqa: F401  (registers the routes)


class _Engine(media.MediaEngine):
    """Accepts everything, records what it was asked to do."""

    name = "engine"

    def __init__(self):
        self.seen: list[dict] = []

    def available(self):
        return True

    def generate_image(self, prompt, **kw):
        return media.MediaResult(ok=True, kind="image", path=_png(),
                                 node_id="n1", url="https://e.invalid/i.png")

    def submit_video(self, prompt, **kw):
        self.seen.append(dict(kw, prompt=prompt))
        return media.MediaResult(ok=True, kind="video", task_id="t-http",
                                 status="queued",
                                 meta={"model": kw.get("model"), "native_audio": False})

    def poll_video(self, task_id, **kw):
        return media.MediaResult(ok=True, kind="video", task_id=task_id,
                                 status="succeeded", path=_mp4(),
                                 url="https://e.invalid/v.mp4")

    def synthesize_speech(self, text, **kw):
        return media.MediaResult(ok=True, kind="audio", path=_mp3())


def _png() -> str:
    p = os.path.join(os.environ["OPS_DATA_DIR"], "media", "p", "a.png")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(b"\x89PNG\r\n\x1a\nx")
    return p


def _mp4() -> str:
    p = os.path.join(os.environ["OPS_DATA_DIR"], "media", "p", "a.mp4")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(b"x")
    return p


def _mp3() -> str:
    p = os.path.join(os.environ["OPS_DATA_DIR"], "media", "p", "a.mp3")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(b"x")
    return p


@pytest.fixture
def server(tmp_path, monkeypatch):
    from ops_dashboard import db
    from ops_dashboard.server import DashboardHandler

    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(media.app_paths, "data_dir", lambda: tmp_path)
    db.init_db()
    engine = _Engine()
    media.set_engine(engine)

    class _Quiet(DashboardHandler):
        def log_message(self, *_a):
            pass

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", engine
    finally:
        httpd.shutdown()
        media.set_engine(None)


def _post(base: str, path: str, payload: dict):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ── status tells the client what it may offer ──────────────────────────────

def test_status_carries_the_capabilities_the_form_needs(server):
    base, _ = server
    status, data = _get(base, "/api/media/status")
    assert status == 200
    assert data["ok"] is True
    caps = {c["model"]: c for c in data["video_capabilities"]}
    assert caps["MiniMax-Hailuo-2.3"]["resolutions_by_duration"]["6"] == ["768P", "1080P"]
    assert data["keyframe_formats"]


# ── video with a keyframe ──────────────────────────────────────────────────

def test_video_accepts_an_image_path_inside_the_media_library(server):
    base, engine = server
    status, data = _post(base, "/api/media/video", {
        "prompt": "slow push in", "model": "MiniMax-H3", "duration": 6,
        "image_path": _png(), "project_key": "p",
    })
    assert status == 200, data
    assert data["ok"] is True
    assert engine.seen[-1]["image_path"] == os.path.realpath(_png())
    assert engine.seen[-1]["reference_type"] == "first_frame"


def test_video_refuses_a_keyframe_outside_the_media_library(server, tmp_path):
    """A crafted path must not hand an arbitrary file to the engine.

    And it must not be dropped either: a silent fallback would render
    text-to-video and bill the caller for a clip they did not ask for.
    """
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"secret")
    base, engine = server
    status, data = _post(base, "/api/media/video", {
        "prompt": "x", "duration": 6, "image_path": str(outside),
    })
    assert status == 400, data
    assert "media library" in data["message"]
    assert engine.seen == []


def test_video_refuses_a_path_outside_every_root(server):
    base, engine = server
    status, data = _post(base, "/api/media/video", {
        "prompt": "x", "duration": 6, "image_path": "/etc/hosts",
    })
    assert status == 400
    assert engine.seen == []


def test_a_missing_keyframe_is_an_error_not_a_text_to_video(server):
    base, engine = server
    status, _data = _post(base, "/api/media/video", {
        "prompt": "x", "duration": 6, "image_path": "/tmp/does-not-exist.png",
    })
    assert status == 400
    assert engine.seen == []


def test_video_needs_a_prompt(server):
    base, _ = server
    status, data = _post(base, "/api/media/video", {"duration": 6})
    assert status == 400
    assert "prompt" in data["message"]


def test_video_rejects_a_duration_the_engine_would_refuse(server):
    """Caught before submission: no credits are spent on a doomed render."""
    base, engine = server
    status, data = _post(base, "/api/media/video", {
        "prompt": "x", "model": "MiniMax-Hailuo-2.3", "duration": 8,
    })
    assert status == 502
    assert engine.seen == []


# ── animate: the one-call image-to-video path ──────────────────────────────

def test_animate_accepts_a_numeric_asset_id(server):
    """Ids arrive as JSON numbers and used to read as absent."""
    asset_id = db_media.add_asset(kind="image", prompt="orb", path=_png(), status="ready")
    base, engine = server
    status, data = _post(base, "/api/media/animate", {
        "asset_id": asset_id, "prompt": "it bobs", "project_key": "p",
    })
    assert status == 200, data
    assert data["ok"] is True
    assert engine.seen[-1]["image_path"] == os.path.realpath(_png())
    assert data["keyframe"]


def test_animate_works_without_a_prompt(server):
    """A bare "animate this" button sends no text; it still has to work."""
    asset_id = db_media.add_asset(kind="image", prompt="orb", path=_png(), status="ready")
    base, engine = server
    status, data = _post(base, "/api/media/animate", {"asset_id": asset_id})
    assert status == 200, data
    assert engine.seen[-1]["prompt"] == routes_media.ANIMATE_DEFAULT_PROMPT


def test_animate_reports_an_unknown_asset(server):
    base, _ = server
    status, data = _post(base, "/api/media/animate", {"asset_id": 99999})
    assert status == 404


def test_animate_needs_to_know_what_to_animate(server):
    base, _ = server
    status, data = _post(base, "/api/media/animate", {"prompt": "bobs"})
    assert status == 400


def test_animate_defaults_to_the_model_the_plan_can_pay_for(server):
    """Hailuo: the H3 family is not covered by a legacy Token Plan."""
    asset_id = db_media.add_asset(kind="image", prompt="orb", path=_png(), status="ready")
    base, engine = server
    _post(base, "/api/media/animate", {"asset_id": asset_id})
    assert engine.seen[-1]["model"] == media.FALLBACK_VIDEO_MODEL


# ── only this Mac may spend credits ────────────────────────────────────────

def test_media_is_not_reachable_through_a_foreign_host(server):
    base, _ = server
    req = urllib.request.Request(base + "/api/media/image", data=b'{"prompt":"x"}',
                                 headers={"Content-Type": "application/json",
                                          "Host": "evil.example.com"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    # The server rejects an unknown Host before routing (421), which is even
    # earlier than the media routes' own 403. Either way: never a render.
    assert status in (403, 421)


# ── the image endpoint keeps its contract ───────────────────────────────────

def test_image_endpoint_records_the_asset(server):
    base, _ = server
    status, data = _post(base, "/api/media/image", {"prompt": "an orb", "project_key": "p"})
    assert status == 200, data
    assert data["ok"] is True
    assert db_media.list_assets(project_key="p")


def test_image_endpoint_needs_a_prompt(server):
    base, _ = server
    status, _data = _post(base, "/api/media/image", {"project_key": "p"})
    assert status == 400


def test_image_endpoint_rejects_an_unknown_aspect_ratio(server):
    base, engine = server
    status, _data = _post(base, "/api/media/image",
                          {"prompt": "x", "aspect_ratio": "7:5"})
    assert status == 502
    assert engine.seen == []