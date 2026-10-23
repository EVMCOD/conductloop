"""Media routes: images, video and speech.

Registered on the shared ``router`` (2026-10-23). The product-facing API:

* ``GET  /api/media/status``            — is the engine usable? (CLI, ffmpeg)
* ``POST /api/media/image``             — text-to-image (synchronous)
* ``POST /api/media/video``             — submit a video job (asynchronous)
* ``GET  /api/media/video/<job_id>``    — poll a video job
* ``POST /api/media/speech``            — text-to-speech
* ``POST /api/media/assemble``          — cut clips into one video (ffmpeg)
* ``GET  /api/media/assets``            — what has been generated

Video is asynchronous on purpose: MiniMax-H3 takes minutes to render, so the
client gets a job id immediately and polls it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import db_media, media
from .routing import router

_WEB = Path(__file__).resolve().parent / "web"


@router.get("/media")
def media_page(handler: Any) -> None:
    """The media workbench: generate, vary, animate, and browse the library.

    Without this page the engine is reachable only over the API. 2026-10-23.
    """
    handler._send(200, (_WEB / "media.html").read_bytes(), "text/html; charset=utf-8")


MAX_BODY = 256 * 1024
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}


def _send_json(handler: Any, status: int, obj: dict) -> None:
    handler._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                  "application/json; charset=utf-8")


def _err(handler: Any, status: int, code: str, message: str = "") -> None:
    _send_json(handler, status, {"ok": False, "error": code, "message": message or code})


def _body(handler: Any) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
    except ValueError:
        length = -1
    if length < 0 or length > MAX_BODY:
        raise ValueError(f"body must be <= {MAX_BODY} bytes")
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError("body must be a JSON object") from e
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    return data


def _host_ok(handler: Any) -> bool:
    """Media spends API credits: only this Mac's own pages may call it."""
    host = (handler.headers.get("Host") or "").strip().lower()
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name in _LOOPBACK_HOSTS


def _str(data: dict, key: str, default: str = "") -> str:
    val = data.get(key, default)
    if isinstance(val, str):
        return val.strip()
    # Ids arrive as JSON numbers. Silently treating them as "absent" made
    # every "animate this asset" call fail with "asset_id is required".
    if isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        return str(val)
    return default


def _resolve_source(source: str) -> str | None:
    """Turn an asset id or a path into a local file inside the media library.

    The id is the safe form; a path is only accepted when it already lives
    under the product's own data directory, so a crafted request cannot ask
    the engine to upload an arbitrary file off this Mac.
    """
    if source.isdigit():
        asset = db_media.get_asset(int(source))
        return (asset or {}).get("path")
    return media.allowed_media_path(source, must_exist=True)


def _keyframe(data: dict, field: str, asset_field: str) -> str | None:
    """Resolve an optional keyframe sent as ``<asset_field>`` or ``<field>``.

    A keyframe that cannot be resolved is an error, never a silent drop: the
    render would otherwise fall back to text-to-video and spend real credits
    on a clip the caller did not ask for.
    """
    asset_id = data.get(asset_field)
    path = _resolve_source(str(asset_id)) if asset_id not in (None, "") else None
    if path:
        return path
    raw = _str(data, field)
    if not raw:
        return None
    path = media.allowed_media_path(raw, must_exist=True)
    if not path:
        if not os.path.isfile(os.path.expanduser(raw)):
            raise ValueError(f"{field}: file not found: {raw}")
        raise ValueError(
            f"{field}: only files from the ConductLoop media library can be used: {raw}"
        )
    return path


@router.get("/api/media/status")
def media_status(handler: Any) -> None:
    """Whether media generation is usable right now, and with what."""
    _send_json(handler, 200, {"ok": True, **media.engine_status()})


@router.get("/api/media/providers")
def media_providers(handler: Any) -> None:
    """Every provider, what it can do, and whether it is usable right now.

    The page builds its pickers from this, so adding a backend needs no
    change in the interface.
    """
    from ops_dashboard import db_media

    _send_json(handler, 200, {
        "ok": True,
        "providers": media.provider_catalogue(),
        "capabilities": media.providers.capability_map(),
        "enabled": media.providers.enabled_keys(),
        "video_options": media.provider_video_options(),
        "usage": db_media.provider_usage_today(),
    })


@router.post("/api/media/providers/<key>")
def media_provider_save(handler: Any, key: str) -> None:
    """Configure a provider, or switch it on and off.

    The API key is write-only: it goes in and never comes back out, and a
    request that leaves it empty keeps the one that is stored.
    """
    if not _host_ok(handler):
        _err(handler, 403, "forbidden", "provider settings are only reachable from this Mac")
        return
    try:
        data = _body(handler)
        key = str(key or "").strip()
        if media.providers.spec_for(key) is None:
            _err(handler, 404, "not_found", f"no provider called '{key}'")
            return
        config = data.get("config")
        if config is not None and not isinstance(config, dict):
            raise ValueError("config must be an object")
        enabled = data.get("enabled")
        priority = data.get("priority")
        media.providers.save_config(
            key,
            config or {},
            enabled=None if enabled is None else bool(enabled),
            priority=int(priority) if priority is not None else None,
        )
        if enabled is not None:
            media.providers.set_enabled(key, bool(enabled))
    except ValueError as e:
        _err(handler, 400, "bad_request", str(e))
        return
    except media.MediaError as e:
        _err(handler, 400, "bad_request", str(e))
        return
    entry = next((p for p in media.provider_catalogue() if p["key"] == key), None)
    _send_json(handler, 200, {"ok": True, "provider": entry})


@router.delete("/api/media/providers/<key>")
def media_provider_disable(handler: Any, key: str) -> None:
    """Switch a provider off. Its configuration is kept."""
    if not _host_ok(handler):
        _err(handler, 403, "forbidden", "provider settings are only reachable from this Mac")
        return
    key = str(key or "").strip()
    if media.providers.spec_for(key) is None:
        _err(handler, 404, "not_found", f"no provider called '{key}'")
        return
    media.providers.set_enabled(key, False)
    _send_json(handler, 200, {"ok": True, "key": key, "enabled": False})


@router.post("/api/media/image")
def media_image(handler: Any) -> None:
    """Generate an image and record it as an asset.

    ``preset`` + ``subject`` expand a ready-made prompt (see
    ``/api/media/presets``); ``image_path``/``image_asset_id`` make it a
    variation of an existing image.
    """
    if not _host_ok(handler):
        _err(handler, 403, "forbidden", "media generation is only reachable from this Mac")
        return
    try:
        data = _body(handler)
        prompt = _str(data, "prompt")
        aspect = _str(data, "aspect_ratio", "1:1") or "1:1"
        preset = _str(data, "preset")
        if preset:
            if prompt:
                raise ValueError("send either a prompt or a preset, not both")
            expanded = media.apply_preset(preset, _str(data, "subject"),
                                          aspect_ratio=_str(data, "aspect_ratio") or None,
                                          background=_str(data, "background") or None)
            prompt, aspect = expanded["prompt"], expanded["aspect_ratio"]
        if not prompt:
            _err(handler, 400, "bad_request", "prompt (or preset + subject) is required")
            return
        res = media.generate_image(
            prompt,
            project_key=_str(data, "project_key") or None,
            aspect_ratio=aspect,
            resolution=_str(data, "resolution", "1K") or "1K",
            n=int(data.get("n") or 1),
            image_path=_keyframe(data, "image_path", "image_asset_id"),
            provider=_str(data, "provider") or None,
        )
    except ValueError as e:
        _err(handler, 400, "bad_request", str(e))
        return
    except media.MediaError as e:
        _err(handler, 502, "engine_error", str(e))
        return
    _send_json(handler, 200, {"ok": True, **res.to_dict()})


@router.get("/api/media/presets")
def media_presets(handler: Any) -> None:
    """The prompt presets the image form offers."""
    _send_json(handler, 200, {"ok": True, "presets": media.prompt_presets()})


@router.post("/api/media/vary")
def media_vary(handler: Any) -> None:
    """Variations of an image already in the library (image-to-image)."""
    if not _host_ok(handler):
        _err(handler, 403, "forbidden", "media generation is only reachable from this Mac")
        return
    try:
        data = _body(handler)
        source = _str(data, "asset_id") or _str(data, "image_path")
        if not source:
            _err(handler, 400, "bad_request", "asset_id is required")
            return
        path = _resolve_source(source)
        if not path:
            _err(handler, 404, "not_found",
                 f"no usable media asset for '{source}': pass an asset_id from "
                 f"the media library, or a file inside it")
            return
        res = media.generate_image(
            _str(data, "prompt") or "Keep the same composition and style",
            project_key=_str(data, "project_key") or None,
            aspect_ratio=_str(data, "aspect_ratio") or "1:1",
            resolution=_str(data, "resolution", "1K") or "1K",
            n=int(data.get("n") or 4),
            image_path=path,
            provider=_str(data, "provider") or None,
        )
        res.meta["reference"] = path
    except ValueError as e:
        _err(handler, 400, "bad_request", str(e))
        return
    except media.MediaError as e:
        _err(handler, 502, "engine_error", str(e))
        return
    _send_json(handler, 200, {"ok": True, **res.to_dict()})


@router.post("/api/media/video")
def media_video(handler: Any) -> None:
    """Submit a video generation. Returns a job id to poll.

    ``image_path`` (or ``asset_id``) animates a still: the keyframe is
    uploaded to the engine and used as the first frame.
    """
    if not _host_ok(handler):
        _err(handler, 403, "forbidden", "media generation is only reachable from this Mac")
        return
    try:
        data = _body(handler)
        prompt = _str(data, "prompt")
        if not prompt:
            _err(handler, 400, "bad_request", "prompt is required")
            return
        out = media.submit_video(
            prompt,
            project_key=_str(data, "project_key") or None,
            model=_str(data, "model", media.DEFAULT_VIDEO_MODEL) or media.DEFAULT_VIDEO_MODEL,
            duration=int(data.get("duration") or 6),
            ratio=_str(data, "ratio", "16:9") or "16:9",
            resolution=_str(data, "resolution") or None,
            image_path=_keyframe(data, "image_path", "image_asset_id"),
            last_frame_path=_keyframe(data, "last_frame_path", "last_frame_asset_id"),
            reference_type=_str(data, "reference_type") or None,
            provider=_str(data, "provider") or None,
        )
    except ValueError as e:
        _err(handler, 400, "bad_request", str(e))
        return
    except media.MediaError as e:
        _err(handler, 502, "engine_error", str(e))
        return
    _send_json(handler, 200, {"ok": True, **out})


# What an "animate this image" button sends when the caller only describes the
# motion it wants. Generic on purpose: it is a starting point to edit, not a
# hidden opinion about the picture.
ANIMATE_DEFAULT_PROMPT = (
    "Slow, natural camera push-in. Gentle ambient motion in the scene, "
    "soft shifting light. Keep the subject unchanged, no text, no artifacts."
)


@router.post("/api/media/animate")
def media_animate(handler: Any) -> None:
    """Animate a generated image: the one-call image-to-video path.

    Takes an ``asset_id`` from the media library rather than a filesystem
    path, so the client never has to know where anything lives.
    """
    if not _host_ok(handler):
        _err(handler, 403, "forbidden", "media generation is only reachable from this Mac")
        return
    try:
        data = _body(handler)
        source = _str(data, "asset_id") or _str(data, "image_path")
        if not source:
            _err(handler, 400, "bad_request", "asset_id is required")
            return
        path = _resolve_source(source)
        if not path:
            # Never fall through to a text-only render: the caller asked to
            # animate something specific and would be billed for a different
            # clip.
            _err(handler, 404, "not_found",
                 f"no usable media asset for '{source}': pass an asset_id from "
                 f"the media library, or a file inside it")
            return
        prompt = _str(data, "prompt") or ANIMATE_DEFAULT_PROMPT
        out = media.animate_image(
            path, prompt,
            project_key=_str(data, "project_key") or None,
            model=_str(data, "model", media.FALLBACK_VIDEO_MODEL) or media.FALLBACK_VIDEO_MODEL,
            duration=int(data.get("duration") or 6),
            resolution=_str(data, "resolution") or None,
            provider=_str(data, "provider") or None,
        )
        out["keyframe"] = path
    except ValueError as e:
        _err(handler, 400, "bad_request", str(e))
        return
    except media.MediaError as e:
        _err(handler, 502, "engine_error", str(e))
        return
    _send_json(handler, 200, {"ok": True, **out})


@router.get("/api/media/video/<job_id>")
def media_video_poll(handler: Any, job_id: str) -> None:
    """Poll a video job. When it is ready the file is downloaded and stored."""
    if not _host_ok(handler):
        _err(handler, 403, "forbidden", "media jobs are only reachable from this Mac")
        return
    try:
        job = media.poll_video(int(job_id))
    except ValueError:
        _err(handler, 400, "bad_request", "job_id must be an integer")
        return
    except media.MediaError as e:
        _err(handler, 502, "engine_error", str(e))
        return
    _send_json(handler, 200, {"ok": True, "job": job})


@router.post("/api/media/speech")
def media_speech(handler: Any) -> None:
    """Synthesize speech from text."""
    if not _host_ok(handler):
        _err(handler, 403, "forbidden", "media generation is only reachable from this Mac")
        return
    try:
        data = _body(handler)
        text = _str(data, "text")
        if not text:
            _err(handler, 400, "bad_request", "text is required")
            return
        res = media.synthesize_speech(
            text,
            project_key=_str(data, "project_key") or None,
            voice_id=_str(data, "voice_id") or None,
            provider=_str(data, "provider") or None,
        )
    except ValueError as e:
        _err(handler, 400, "bad_request", str(e))
        return
    except media.MediaError as e:
        _err(handler, 502, "engine_error", str(e))
        return
    _send_json(handler, 200, {"ok": True, **res.to_dict()})


@router.post("/api/media/assemble")
def media_assemble(handler: Any) -> None:
    """Cut clips into one video, with optional music bed and subtitles."""
    if not _host_ok(handler):
        _err(handler, 403, "forbidden", "media assembly is only reachable from this Mac")
        return
    try:
        data = _body(handler)
        clips = data.get("clips") or []
        if not isinstance(clips, list) or not clips:
            _err(handler, 400, "bad_request", "clips must be a non-empty list of paths")
            return
        out_path = media.media_dir(_str(data, "project_key") or None) / "assembled.mp4"
        path = media.assemble(
            [str(c) for c in clips],
            out_path,
            music=_str(data, "music") or None,
            subtitles=_str(data, "subtitles") or None,
        )
        asset_id = db_media.add_asset(
            kind="video", prompt=_str(data, "prompt") or "assembled",
            project_key=_str(data, "project_key") or None, engine="ffmpeg",
            path=path, status="ready", meta={"clips": len(clips)},
        )
    except ValueError as e:
        _err(handler, 400, "bad_request", str(e))
        return
    except media.MediaError as e:
        _err(handler, 502, "engine_error", str(e))
        return
    _send_json(handler, 200, {"ok": True, "path": path, "asset_id": asset_id})


_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".heic": "image/heic", ".mp4": "video/mp4",
    ".mov": "video/quicktime", ".webm": "video/webm", ".mp3": "audio/mpeg",
    ".wav": "audio/wav", ".m4a": "audio/mp4",
}


@router.get("/api/media/file/<asset_id>")
def media_file(handler: Any, asset_id: str) -> None:
    """Serve a generated file by id.

    The gallery renders ``/api/media/file/12`` rather than the path on disk,
    so a file can never be addressed from outside the media library and the
    id stays meaningful if the layout ever moves.
    """
    if not _host_ok(handler):
        _err(handler, 403, "forbidden", "media files are only reachable from this Mac")
        return
    try:
        aid = int(asset_id)
    except ValueError:
        _err(handler, 400, "bad_request", "asset_id must be an integer")
        return
    asset = db_media.get_asset(aid)
    path = media.allowed_media_path((asset or {}).get("path") or "")
    if not path:
        _err(handler, 404, "not_found", "no such media asset")
        return
    ext = os.path.splitext(path)[1].lower()
    ctype = _MEDIA_TYPES.get(ext, "application/octet-stream")
    handler._send(200, Path(path).read_bytes(), ctype)


@router.delete("/api/media/assets/<asset_id>")
def media_delete_asset(handler: Any, asset_id: str) -> None:
    """Remove an asset from the library, and its file from disk.

    The file goes with the row: a deleted row pointing at a file on disk is
    an orphan nobody will ever clean up, and the media directory grows for
    ever otherwise.
    """
    if not _host_ok(handler):
        _err(handler, 403, "forbidden", "media is only reachable from this Mac")
        return
    try:
        aid = int(asset_id)
    except ValueError:
        _err(handler, 400, "bad_request", "asset_id must be an integer")
        return
    asset = db_media.get_asset(aid)
    if not asset:
        _err(handler, 404, "not_found", "no such media asset")
        return
    # An asset that a video was built from stays: deleting the keyframe would
    # break the record of what the clip started from.
    path = media.allowed_media_path(asset.get("path") or "")
    removed = False
    if path:
        try:
            os.remove(path)
            removed = True
        except FileNotFoundError:
            pass
        except OSError as e:
            _err(handler, 500, "delete_failed", f"could not remove the file: {e}")
            return
    db_media.delete_asset(aid)
    _send_json(handler, 200, {"ok": True, "id": aid, "file_removed": removed})


@router.post("/api/media/voiceover")
def media_voiceover(handler: Any) -> None:
    """Give a clip a spoken narration: script in, narrated video out.

    The clip does not have to be silent — this replaces its audio — but it
    is meant for the models that render no sound at all.
    """
    if not _host_ok(handler):
        _err(handler, 403, "forbidden", "media generation is only reachable from this Mac")
        return
    try:
        data = _body(handler)
        source = _str(data, "asset_id") or _str(data, "video_path")
        if not source:
            _err(handler, 400, "bad_request", "asset_id is required")
            return
        path = _resolve_source(source)
        if not path:
            _err(handler, 404, "not_found",
                 f"no usable media asset for '{source}': pass an asset_id from "
                 f"the media library, or a file inside it")
            return
        script = _str(data, "script")
        if not script:
            _err(handler, 400, "bad_request", "script is required")
            return
        music = _str(data, "music") or None
        if music:
            music = media.allowed_media_path(music, must_exist=True) or None
        res = media.voiceover(
            path, script,
            project_key=_str(data, "project_key") or None,
            voice_id=_str(data, "voice_id") or None,
            emotion=_str(data, "emotion") or None,
            speed=(float(data["speed"]) if data.get("speed") is not None else None),
            music=music,
            provider=_str(data, "provider") or None,
        )
    except ValueError as e:
        _err(handler, 400, "bad_request", str(e))
        return
    except media.MediaError as e:
        _err(handler, 502, "engine_error", str(e))
        return
    _send_json(handler, 200, {"ok": True, **res.to_dict()})


@router.get("/api/media/assets")
def media_assets(handler: Any) -> None:
    """List what has been generated."""
    if not _host_ok(handler):
        _err(handler, 403, "forbidden", "media assets are only reachable from this Mac")
        return
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(handler.path).query)
    project = (qs.get("project_key") or [None])[0]
    kind = (qs.get("kind") or [None])[0]
    try:
        limit = int((qs.get("limit") or ["100"])[0])
    except ValueError:
        limit = 100
    rows = db_media.list_assets(project_key=project, kind=kind, limit=min(limit, 500))
    jobs = db_media.list_jobs(project_key=project, limit=min(limit, 500))
    _send_json(handler, 200, {"ok": True, "rows": rows, "jobs": jobs})
