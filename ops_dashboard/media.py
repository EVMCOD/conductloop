"""Media generation for ConductLoop: images, video and speech.

Where the generation happens
----------------------------
The engine is the **Matrix connector**, reached through the ``mcode-tools``
CLI that ships with the MiniMax desktop runtime. It exposes:

===========================  ================================================
``generate_image``           text-to-image / image-to-image (1K, 2K, 4K)
``submit_video_generation``  asynchronous video (MiniMax-H3 / H3-Max / Hailuo)
``query_video_generation``   poll a video task
``synthesize_speech``        text-to-speech
===========================  ================================================

Why an engine abstraction
-------------------------
Video generation takes minutes (MiniMax-H3: 15-30 min; H3-Max: ~20 s) and
returns a task id, not a file. ConductLoop therefore keeps its own job table
(``media_jobs``) so the product can show "generating…" instead of holding an
HTTP request open, and its own asset table (``media_assets``) so every
produced file is traceable to the prompt, the engine and the project it
belongs to.

Nothing in here talks to the network at import time, and every entry point
degrades to a clear error instead of an exception when the CLI is missing.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ops_dashboard import app_paths
from ops_dashboard import media_providers as providers
from ops_dashboard.media_providers import MediaError, MediaResult

# One definition of the error and the result, shared with the provider
# framework: the product, the routes and the backends must agree on what a
# failure is, or a fallback cannot be told from a crash. 2026-10-23.
__all_media_types__ = (MediaError, MediaResult)

# ── configuration ──────────────────────────────────────────────────────────

IMAGE_TOOL = "connector__matrix__generate_image"
VIDEO_SUBMIT_TOOL = "connector__matrix__submit_video_generation"
VIDEO_QUERY_TOOL = "connector__matrix__query_video_generation"
SPEECH_TOOL = "connector__matrix__synthesize_speech"

ASPECT_RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4", "21:9")
IMAGE_RESOLUTIONS = ("1K", "2K", "4K")

# ── video capabilities, per model ──────────────────────────────────────────
#
# Read off the live `submit_video_generation` schema (2026-10-23) and checked
# against the API with real submissions. The rules are not uniform, and
# guessing them is how you lose a paid render:
#
#   * only the H3 family accepts ``ratio``; Hailuo-2.3 rejects it outright;
#   * H3-Max caps at 768P and takes no reference collections;
#   * Hailuo-2.3 only allows 1080P at 6 seconds;
#   * in keyframe mode the aspect always follows the source image
#     ("adaptive"), so sending a ratio there is meaningless.
#
# The product reads these tables to build a form that can only submit valid
# combinations, instead of rejecting a render after paying for it.

VIDEO_RATIOS = ("adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16")


@dataclass(frozen=True)
class VideoSpec:
    """What one video model can actually do."""

    name: str
    durations: tuple[int, ...]              # exact allowed seconds
    duration_range: tuple[int, int]         # inclusive range, for the slider
    resolutions: tuple[str, ...]            # 1080P only for 6s on Hailuo
    resolution_by_duration: dict[int, tuple[str, ...]] = field(default_factory=dict)
    accepts_ratio: bool = True
    native_audio: bool = False              # renders a synchronized sound track
    keyframe: tuple[str, ...] = ()          # first_frame | last_frame | subject
    references: bool = False                # image / video / audio collections
    fallback: str | None = None             # cheaper model on credit refusal

    def resolutions_for(self, duration: int) -> tuple[str, ...]:
        return self.resolution_by_duration.get(int(duration), self.resolutions)

    def accepts_duration(self, duration: int) -> bool:
        if self.durations:
            return int(duration) in self.durations
        lo, hi = self.duration_range
        return lo <= int(duration) <= hi


VIDEO_SPECS: dict[str, VideoSpec] = {
    "MiniMax-H3": VideoSpec(
        name="MiniMax-H3",
        durations=(), duration_range=(4, 15),
        resolutions=("768P", "2K"),
        native_audio=True, keyframe=("first_frame", "last_frame"), references=True,
    ),
    "MiniMax-H3-Max": VideoSpec(
        name="MiniMax-H3-Max",
        durations=(), duration_range=(5, 15),
        resolutions=("480P", "768P"),
        native_audio=True, keyframe=("first_frame", "last_frame"),
    ),
    "MiniMax-Hailuo-2.3": VideoSpec(
        name="MiniMax-Hailuo-2.3",
        durations=(6, 10), duration_range=(6, 10),
        resolutions=("768P",),
        resolution_by_duration={6: ("768P", "1080P"), 10: ("768P",)},
        accepts_ratio=False,
        keyframe=("first_frame", "subject"),
        fallback=None,
    ),
}

# MiniMax-H3 renders a synchronized native audio track, so a video does not
# need a separate text-to-speech pass. H3-Max is the fast variant (~20 s).
# Hailuo-2.3 is the only model a legacy Token Plan can pay for.
DEFAULT_VIDEO_MODEL = "MiniMax-H3"
FAST_VIDEO_MODEL = "MiniMax-H3-Max"
FALLBACK_VIDEO_MODEL = "MiniMax-Hailuo-2.3"
VIDEO_MODELS = tuple(VIDEO_SPECS)

# Kept for callers that only need the bounds; Hailuo's discrete durations are
# enforced by VideoSpec.accepts_duration().
VIDEO_DURATION_LIMITS = {
    name: (spec.duration_range if not spec.durations else (min(spec.durations), max(spec.durations)))
    for name, spec in VIDEO_SPECS.items()
}

KEYFRAME_MODES = ("first_frame", "last_frame", "subject")

# A keyframe is uploaded to the engine; these are the engine's own limits.
KEYFRAME_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
}
KEYFRAME_MAX_BYTES = 30 * 1024 * 1024

DEFAULT_VOICE_ID = "male-qn-qingse"

# Where the CLI may live when it is not on PATH (packaged .app, launchd jobs).
_CLI_CANDIDATES = (
    "mcode-tools",
    os.path.expanduser("~/.minimax/bin/mcode-tools"),
    "/opt/homebrew/bin/mcode-tools",
    "/usr/local/bin/mcode-tools",
)

# The connector downloads "output_file" into the current working directory.
# We always call it with a temp cwd and then move the file where we want it.
_CALL_TIMEOUT = 300


# MediaError and MediaResult are imported from media_providers above: one
# definition for the product, the routes and every backend, so a provider
# failure can be told apart from a crash anywhere in the chain. 2026-10-23.


# ── filesystem layout ──────────────────────────────────────────────────────

def media_dir(project_key: str | None = None) -> Path:
    """Where generated media lives: <data dir>/media[/<project>]."""
    base = app_paths.data_dir() / "media"
    if project_key:
        safe = "".join(c for c in project_key if c.isalnum() or c in "-_")
        if safe:
            base = base / safe
    base.mkdir(parents=True, exist_ok=True)
    return base


def _slug(text: str, maxlen: int = 48) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (text or "").lower())
    out = "-".join(filter(None, out.split("-")))
    return (out[:maxlen] or "asset").strip("-")


def _unique_name(text: str, ext: str) -> str:
    """A file name that does not collide with a previous generation.

    Two generations of the same prompt used to write to the same path, so the
    second overwrote the first and the asset table pointed twice at one
    file. 2026-10-23.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rand = uuid.uuid4().hex[:8]
    return f"{_slug(text)}-{stamp}-{rand}{ext}"


# ── the engine ─────────────────────────────────────────────────────────────

def _ensure_fd_headroom() -> None:
    """Raise the soft file-descriptor limit to the hard limit.

    Jobs launched by launchd (the digest, the boardroom recap) and the
    packaged desktop app inherit a soft limit of 256 open files. The packaged
    app is a PyInstaller one-file bundle that keeps its extracted files open,
    so spawning a subprocess with pipes died with EMFILE ("Too many open
    files") and image generation failed there while working in the dev
    checkout. 2026-10-23.
    """
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    except Exception:
        pass


class MediaEngine:
    """Base class: an engine that can produce images, video and speech."""

    name = "none"

    def available(self) -> bool:
        return False

    def generate_image(self, prompt: str, **kw) -> MediaResult:
        raise MediaError(f"engine '{self.name}' cannot generate images")

    def submit_video(self, prompt: str, **kw) -> MediaResult:
        raise MediaError(f"engine '{self.name}' cannot generate video")

    def poll_video(self, task_id: str, **kw) -> MediaResult:
        raise MediaError(f"engine '{self.name}' cannot poll video jobs")

    def synthesize_speech(self, text: str, **kw) -> MediaResult:
        raise MediaError(f"engine '{self.name}' cannot synthesize speech")

    def upload(self, path: str, *, mime_type: str | None = None) -> str:
        """Publish a local file and return a temporary public url.

        Image-to-video needs the keyframe on the engine's side, and the
        connector only accepts urls: a raw path, a data URI or base64 are
        rejected outright. 2026-10-23.
        """
        raise MediaError(f"engine '{self.name}' cannot upload local media")


def find_cli() -> str | None:
    """Locate the mcode-tools CLI, even when PATH is not inherited."""
    for cand in _CLI_CANDIDATES:
        if os.path.isabs(cand):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


class MatrixEngine(MediaEngine):
    """The Matrix connector, driven through the mcode-tools CLI."""

    name = "matrix"

    def __init__(self, cli: str | None = None):
        self._cli = cli or find_cli()

    def available(self) -> bool:
        return bool(self._cli)

    def _require(self) -> str:
        if not self._cli:
            raise MediaError(
                "mcode-tools CLI not found. The Matrix media engine needs the "
                "MiniMax desktop runtime (expected 'mcode-tools' on PATH)."
            )
        return self._cli

    def _run(self, args: list[str], *, timeout: int = _CALL_TIMEOUT,
             cwd: str | None = None) -> str:
        """Run a CLI command and return stdout."""
        cli = self._require()
        _ensure_fd_headroom()
        try:
            proc = subprocess.run(
                [cli, *args], capture_output=True, text=True, timeout=timeout,
                cwd=cwd or tempfile.gettempdir(),
            )
        except subprocess.TimeoutExpired as e:
            raise MediaError(f"mcode-tools timed out after {timeout}s") from e
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise MediaError(f"mcode-tools failed ({proc.returncode}): {err[:400]}")
        return proc.stdout

    def _call(self, tool: str, args: dict[str, Any], *, timeout: int = _CALL_TIMEOUT) -> Any:
        """Call one connector tool with a JSON payload and parse the result."""
        # Private temp dir per call, removed afterwards: the connector writes
        # its "output_file" into the current directory, and a shared /tmp name
        # would leak files and let another process swap them (2026-10-23).
        workdir = tempfile.mkdtemp(prefix="conductloop-media-call-")
        args_file = os.path.join(workdir, "args.json")
        try:
            with open(args_file, "w", encoding="utf-8") as fh:
                json.dump(args, fh)
            out = self._run(["connector", "call", tool, "--args-file", args_file],
                            timeout=timeout, cwd=workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        out = (out or "").strip()
        if not out:
            raise MediaError(f"{tool} returned no output")
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise MediaError(f"{tool} returned non-JSON output: {out[:200]}") from e
        # The engine reports failures in the payload, not in the exit code: a
        # refused render came back as {"code": 402, "message": ...} with a zero
        # exit status, so the product used to read it as a successful submit
        # and only fail later with "returned no task_id". 2026-10-23.
        if isinstance(data, dict):
            code = data.get("code")
            if isinstance(code, int) and code != 0:
                detail = (data.get("message") or data.get("error")
                          or data.get("failure_details") or f"engine error {code}")
                raise MediaError(str(detail)[:600], code=code)
        return data

    # ── images ────────────────────────────────────────────────────────────

    def generate_image(self, prompt: str, *, aspect_ratio: str = "1:1",
                       resolution: str = "1K", project_key: str | None = None,
                       n: int = 1, image_path: str | None = None, **_) -> MediaResult:
        """Generate an image, optionally from a reference image.

        ``image_path`` makes this image-to-image: the local file is uploaded
        and passed as ``input_urls``, so the render follows its composition,
        palette and style. 2026-10-23.
        """
        if aspect_ratio not in ASPECT_RATIOS:
            raise MediaError(f"aspect_ratio must be one of: {', '.join(ASPECT_RATIOS)}")
        if resolution not in IMAGE_RESOLUTIONS:
            raise MediaError(f"resolution must be one of: {', '.join(IMAGE_RESOLUTIONS)}")
        if not (prompt or "").strip():
            raise MediaError("prompt is required")

        count = max(1, min(int(n or 1), 4))
        # Uploaded once for the whole batch: the reference is the same for
        # every variation, and each upload is a round trip to the engine.
        input_urls = [self.upload(image_path)] if image_path else None
        request: dict[str, Any] = {"prompt": prompt, "aspect_ratio": aspect_ratio,
                                   "resolution": resolution}
        if input_urls:
            request["input_urls"] = input_urls

        data = self._call(IMAGE_TOOL, {"requests": [dict(request) for _ in range(count)]})
        items = data.get("success_items") or data.get("items") or []
        if not items:
            failed = data.get("failed_items") or data.get("failure_items")
            if failed:
                raise MediaError(f"image generation failed: {str(failed)[:300]}")
            raise MediaError("image generation returned no items")

        first = items[0]
        node_id = first.get("node_id")
        url = self._asset_url(node_id) if node_id else None
        path = None
        paths: list[str] = []
        # Every generated image is downloaded and kept: generating four and
        # storing one meant paying for four and keeping one. 2026-10-23.
        for item in items[:count]:
            item_url = url if item is first else self._asset_url(item.get("node_id"))
            if not item_url:
                continue
            dest = media_dir(project_key) / _unique_name(prompt, ".png")
            saved = self._download(item_url, dest)
            paths.append(str(saved))
            if path is None:
                path = str(saved)
        return MediaResult(
            ok=True, kind="image", path=path,
            node_id=node_id, url=url,
            meta={"items": len(items), "paths": paths, "saved": len(paths),
                  "aspect_ratio": aspect_ratio, "resolution": resolution,
                  "reference": image_path, "variation": bool(input_urls),
                  "file_name": first.get("file_name")},
        )

    def _asset_url(self, node_id: str) -> str | None:
        """Short-lived download url for a generated asset."""
        try:
            out = self._run(["get-asset-url", str(node_id)], timeout=60)
            data = json.loads(out)
        except (MediaError, json.JSONDecodeError):
            return None
        return data.get("download_url") or data.get("url")

    def _download(self, url: str, dest: Path) -> Path:
        """Download a generated file, with a size cap (a video can be big)."""
        max_bytes = 512 * 1024 * 1024
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                length = r.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise MediaError(f"the generated file is too big ({int(length)} bytes)")
                data = r.read(max_bytes + 1)
        except MediaError:
            raise
        except (urllib.error.URLError, OSError) as e:
            raise MediaError(f"could not download the generated file: {e}") from e
        if len(data) > max_bytes:
            raise MediaError("the generated file is too big")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    # ── uploads ───────────────────────────────────────────────────────────

    def upload(self, path: str, *, mime_type: str | None = None) -> str:
        """Publish a local file to the connector and return its temp url."""
        self._require()
        # Absolute, always: the CLI runs in a private temp cwd, so a relative
        # path from the caller resolves to nothing and the upload fails.
        src = Path(path).expanduser().resolve()
        if not src.is_file():
            raise MediaError(f"file not found: {path}")
        size = src.stat().st_size
        if size > KEYFRAME_MAX_BYTES:
            raise MediaError(
                f"that image is {size / 1e6:.1f} MB; the engine accepts up to 30 MB"
            )
        mime = mime_type or KEYFRAME_MIME.get(src.suffix.lower())
        if mime is None:
            raise MediaError(
                f"unsupported keyframe format '{src.suffix or src.name}'; "
                f"use one of: {', '.join(sorted(KEYFRAME_MIME))}"
            )
        args = ["upload-temp-url", str(src)]
        if mime:
            args += ["--mime-type", mime]
        try:
            data = json.loads(self._run(args, timeout=180))
        except json.JSONDecodeError as e:
            raise MediaError(f"upload returned non-JSON output: {str(data)[:200]}") from e
        url = data.get("temp_url") or data.get("url")
        if not url:
            raise MediaError(f"upload returned no temp url: {str(data)[:200]}")
        return url

    # ── video ─────────────────────────────────────────────────────────────

    def submit_video(self, prompt: str, *, model: str = DEFAULT_VIDEO_MODEL,
                     duration: int = 6, ratio: str = "16:9",
                     resolution: str | None = None,
                     image_path: str | None = None,
                     reference_type: str | None = None,
                     last_frame_path: str | None = None,
                     **_) -> MediaResult:
        """Queue one render. Returns a task id, never a file.

        ``image_path`` turns this into image-to-video: the local keyframe is
        uploaded and handed over as ``input_image``. The engine answers 402
        when the account cannot pay for the H3 family, and that used to kill
        the whole job even though Hailuo-2.3 would have run on the legacy
        plan — so the refusal is retried on the fallback model and reported.
        """
        spec = VIDEO_SPECS.get(model)
        if spec is None:
            raise MediaError(f"model must be one of: {', '.join(VIDEO_MODELS)}")
        duration = int(duration or 0)
        if not spec.accepts_duration(duration):
            allowed = (f"{spec.durations} seconds" if spec.durations
                       else f"{spec.duration_range[0]}-{spec.duration_range[1]} seconds")
            raise MediaError(f"{model} takes {allowed}, not {duration}")
        if not (prompt or "").strip():
            raise MediaError("prompt is required")
        if len(prompt) > 7000:
            raise MediaError("the video prompt is limited to 7000 characters")
        if resolution and resolution not in spec.resolutions_for(duration):
            raise MediaError(
                f"{model} at {duration}s does not take {resolution}; "
                f"use {' or '.join(spec.resolutions_for(duration))}"
            )
        if ratio and ratio not in VIDEO_RATIOS:
            raise MediaError(f"ratio must be one of: {', '.join(VIDEO_RATIOS)}")

        keyframe = bool(image_path)
        mode = reference_type or ("first_frame" if keyframe else None)
        if mode and mode not in KEYFRAME_MODES:
            raise MediaError(f"reference_type must be one of: {', '.join(KEYFRAME_MODES)}")
        if mode and mode not in spec.keyframe:
            raise MediaError(f"{model} does not support the '{mode}' keyframe mode")
        if last_frame_path and "last_frame" not in spec.keyframe:
            raise MediaError(f"{model} does not support a last frame")
        if last_frame_path and not keyframe:
            raise MediaError("a last frame needs a first frame")

        uploaded = image_path or last_frame_path
        # In keyframe mode the aspect follows the source image ("adaptive"),
        # so a ratio here is not just redundant, it is wrong.
        args: dict[str, Any] = {"prompt": prompt, "model": model, "duration": duration}
        if resolution:
            args["resolution"] = resolution
        if spec.accepts_ratio:
            # In keyframe mode the aspect follows the source image
            # ("adaptive"), so a ratio there is not just redundant, it is wrong.
            args["ratio"] = "adaptive" if keyframe else (ratio or "16:9")
        if image_path:
            args["input_image"] = {
                "url": self.upload(image_path),
                "mime_type": KEYFRAME_MIME.get(Path(image_path).suffix.lower()),
            }
            args["reference_type"] = mode
        if last_frame_path:
            args["last_frame_image"] = {
                "url": self.upload(last_frame_path),
                "mime_type": KEYFRAME_MIME.get(Path(last_frame_path).suffix.lower()),
            }

        requested = model
        try:
            data = self._call(VIDEO_SUBMIT_TOOL, args)
        except MediaError as exc:
            fallback = self._submit_fallback(spec, prompt, duration, args, exc, uploaded)
            if fallback is None:
                raise
            data, model = fallback

        task_id = data.get("task_id") or (data.get("data") or {}).get("task_id")
        if not task_id:
            raise MediaError(f"video submission returned no task_id: {str(data)[:200]}")
        # Re-read the spec: after a fallback the render is running on another
        # model, and claiming H3's native sound track for a silent Hailuo clip
        # is exactly the kind of promise the product must not make.
        spec = VIDEO_SPECS[model]
        meta: dict[str, Any] = {
            "model": model, "duration": duration, "native_audio": spec.native_audio,
            "requested_ratio": ratio, "keyframe": image_path,
            "reference_type": mode, "resolution": resolution or spec.resolutions[0],
        }
        if model != requested:
            meta["requested_model"] = requested
            meta["fell_back"] = True
        if last_frame_path:
            meta["last_frame"] = last_frame_path
        return MediaResult(ok=True, kind="video", task_id=str(task_id), status="queued",
                           meta=meta)

    def _submit_fallback(self, spec: VideoSpec, prompt: str, duration: int,
                         args: dict[str, Any], exc: MediaError,
                         uploaded: str | None) -> tuple[dict[str, Any], str] | None:
        """Retry a credit-refused render on the cheaper model, or give up.

        Only the account's wallet decides which model runs; the product should
        not lose the render over it. 2026-10-23.
        """
        target = FALLBACK_VIDEO_MODEL
        if getattr(exc, "code", None) != 402 or target == spec.name:
            return None
        target_spec = VIDEO_SPECS[target]
        if not target_spec.accepts_duration(duration) or not uploaded:
            return None
        retry = {"prompt": prompt, "model": target, "duration": duration,
                 "resolution": target_spec.resolutions_for(duration)[0]}
        if uploaded:
            retry["input_image"] = {
                "url": self.upload(uploaded),
                "mime_type": KEYFRAME_MIME.get(Path(uploaded).suffix.lower()),
            }
            retry["reference_type"] = "first_frame"
        data = self._call(VIDEO_SUBMIT_TOOL, retry)
        return data, target

    def poll_video(self, task_id: str, *, model: str = DEFAULT_VIDEO_MODEL,
                   project_key: str | None = None, **_) -> MediaResult:
        spec = VIDEO_SPECS.get(model) or VIDEO_SPECS[DEFAULT_VIDEO_MODEL]
        data = self._call(VIDEO_QUERY_TOOL, {"task_id": str(task_id), "model": model})
        status = str(data.get("status") or "unknown").lower()
        video_url = data.get("video_url") or (data.get("data") or {}).get("video_url")
        if status in ("success", "succeeded") and video_url:
            # A unique name: re-polling a finished task must not overwrite the
            # file the asset table already points at.
            path = self._download(video_url, media_dir(project_key) / _unique_name(task_id, ".mp4"))
            return MediaResult(ok=True, kind="video", path=str(path), task_id=str(task_id),
                               url=video_url, status="succeeded",
                               meta={"model": model, "file_id": data.get("file_id"),
                                     "native_audio": spec.native_audio})
        if status in ("fail", "failed", "cancelled", "canceled"):
            return MediaResult(ok=False, kind="video", task_id=str(task_id), status="failed",
                               error=str(data.get("failure_details") or data.get("message")
                                         or data.get("error") or "video generation failed")[:400])
        return MediaResult(ok=True, kind="video", task_id=str(task_id), status=status,
                           meta={"model": model, "raw_status": status})

    # ── speech ────────────────────────────────────────────────────────────

    def synthesize_speech(self, text: str, *, voice_id: str | None = None,
                          project_key: str | None = None,
                          emotion: str | None = None, speed: float | None = None,
                          pitch: int | None = None, volume: float | None = None,
                          **_) -> MediaResult:
        """Speak some text and keep the audio file.

        The engine answers with a ``node_id`` and a ``file_name``, not with a
        path: the file is fetched through the asset url, the same way the
        generated images are. Looking for "file_path" (as this used to) found
        nothing, so the audio was never stored. 2026-10-23.
        """
        if not (text or "").strip():
            raise MediaError("text is required")
        args: dict[str, Any] = {"text": text}
        if voice_id:
            args["voice_id"] = voice_id
        if emotion:
            args["emotion"] = emotion
        if speed is not None:
            args["speed"] = speed
        if pitch is not None:
            args["pitch"] = pitch
        if volume is not None:
            args["volume"] = volume
        data = self._call(SPEECH_TOOL, args)

        path = None
        # A path in the answer: the CLI downloaded the file itself.
        for key in ("file_path", "path", "output_path"):
            val = data.get(key)
            if val and os.path.isfile(val):
                path = Path(val)
                break
        if path is None:
            # The usual shape: a node id to resolve into a download url.
            node_id = data.get("node_id")
            if not node_id:
                items = data.get("success_items") or []
                if items and isinstance(items[0], dict):
                    node_id = items[0].get("node_id")
            if node_id:
                url = self._asset_url(node_id)
                if url:
                    dest = media_dir(project_key) / _unique_name(text[:40], ".mp3")
                    path = self._download(url, dest)
        if path is None:
            raise MediaError(
                f"speech synthesis returned no file: {str(data)[:200]}"
            )
        dest = media_dir(project_key) / _unique_name(text[:40], ".mp3")
        if path.resolve() != dest.resolve():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        return MediaResult(ok=True, kind="audio", path=str(dest),
                           node_id=data.get("node_id"),
                           meta={"voice_id": voice_id or DEFAULT_VOICE_ID,
                                 "emotion": emotion, "speed": speed,
                                 "pitch": pitch, "volume": volume,
                                 "bytes": dest.stat().st_size})


# ── engine registry ────────────────────────────────────────────────────────

_ENGINE: MediaEngine | None = None


def get_engine() -> MediaEngine:
    """The engine ConductLoop uses. Overridable for tests."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = MatrixEngine()
    return _ENGINE


def set_engine(engine: MediaEngine | None) -> None:
    """Replace the engine (tests, or a different provider later)."""
    global _ENGINE
    _ENGINE = engine


# ── provider selection ─────────────────────────────────────────────────────
#
# The product asks for a capability, never for a vendor. Which provider
# answers is a configuration decision, made in the database, and the logic
# below is the only place that decision is applied. 2026-10-23.


class MatrixVideoContract:
    """The rules of the MiniMax video models.

    These rules belong to the *vendor*, not to the product: they describe
    what MiniMax-H3 and Hailuo really accept. The product asks the provider
    for them instead of hardcoding a table of somebody else's models, and
    any other provider answers with its own.
    """

    def video_options(self) -> dict[str, Any]:
        models = []
        for name, spec in VIDEO_SPECS.items():
            models.append({
                "model": name,
                "durations": list(spec.durations) or list(range(*spec.duration_range)),
                "duration_range": list(spec.duration_range),
                "resolutions": list(spec.resolutions),
                "resolutions_by_duration": {str(k): list(v)
                                            for k, v in spec.resolution_by_duration.items()},
                "accepts_ratio": spec.accepts_ratio,
                "native_audio": spec.native_audio,
                "keyframe_modes": list(spec.keyframe),
                "references": spec.references,
                "default": name == DEFAULT_VIDEO_MODEL,
                "fast": name == FAST_VIDEO_MODEL,
            })
        return {
            "models": models,
            "default_model": DEFAULT_VIDEO_MODEL,
            "fast_model": FAST_VIDEO_MODEL,
            "fallback_model": FALLBACK_VIDEO_MODEL,
            "accepts_ratio": True,
            "keyframe_modes": list(KEYFRAME_MODES),
            "references": True,
        }

    def validate_video(self, *, model: str | None = None, duration: int = 6,
                       resolution: str | None = None, ratio: str | None = None,
                       reference_type: str | None = None,
                       has_image: bool = False) -> None:
        spec = VIDEO_SPECS.get(model or "")
        if spec is None:
            raise MediaError(f"model must be one of: {', '.join(VIDEO_MODELS)}")
        if not spec.accepts_duration(int(duration or 0)):
            allowed = (f"{spec.durations} seconds" if spec.durations
                       else f"{spec.duration_range[0]}-{spec.duration_range[1]} seconds")
            raise MediaError(f"{model} takes {allowed}, not {duration}")
        if resolution and resolution not in spec.resolutions_for(int(duration)):
            raise MediaError(
                f"{model} at {duration}s does not take {resolution}; "
                f"use {' or '.join(spec.resolutions_for(int(duration)))}"
            )
        if ratio and ratio not in VIDEO_RATIOS:
            raise MediaError(f"ratio must be one of: {', '.join(VIDEO_RATIOS)}")
        mode = reference_type or ("first_frame" if has_image else None)
        if mode and mode not in KEYFRAME_MODES:
            raise MediaError(f"reference_type must be one of: {', '.join(KEYFRAME_MODES)}")
        if mode and mode not in spec.keyframe:
            raise MediaError(
                f"{model} has no '{mode}' mode; it takes "
                f"{', '.join(spec.keyframe) or 'no keyframe'}"
            )


class EngineProvider(MatrixVideoContract, providers.Provider):
    """Wraps one of the original engines in the provider contract.

    The MiniMax connector started life as "the engine". It stays exactly as
    capable as it was, but from now on it is *a* provider: one among several,
    chosen the same way as any other. This adapter is what lets the old
    engine keep its own code — and its own tests — while the product stops
    knowing it exists.
    """

    spec = providers.ProviderSpec(
        key="matrix",
        label="MiniMax (Matrix connector)",
        capabilities=(providers.IMAGE, providers.VIDEO, providers.SPEECH),
        kind="remote",
        doc="The MiniMax desktop runtime: images, video and speech through mcode-tools.",
        default_enabled=True,
    )

    def __init__(self, engine: MediaEngine):
        super().__init__({})
        self.engine = engine

    def configured(self) -> bool:
        return bool(getattr(self.engine, "available", lambda: True)())

    @property
    def identity(self) -> str:
        """An asset records the engine's own name, not the adapter's."""
        return getattr(self.engine, "name", "") or self.spec.key

    def health(self) -> tuple[bool, str]:
        if self.configured():
            return True, f"{self.spec.label} is available"
        return False, f"{self.spec.label} is not available"

    # The engine already returns MediaResult: the adapter only stamps which
    # provider did the work.
    def _stamp(self, result: MediaResult) -> MediaResult:
        if isinstance(result, MediaResult):
            result.provider = result.provider or self.identity
        return result

    def generate_image(self, prompt: str, **kw) -> MediaResult:
        return self._stamp(self.engine.generate_image(prompt, **kw))

    def submit_video(self, prompt: str, **kw) -> MediaResult:
        return self._stamp(self.engine.submit_video(prompt, **kw))

    def poll_video(self, task_id: str, **kw) -> MediaResult:
        return self._stamp(self.engine.poll_video(task_id, **kw))

    def synthesize_speech(self, text: str, **kw) -> MediaResult:
        return self._stamp(self.engine.synthesize_speech(text, **kw))

    def upload(self, path: str, *, mime_type: str | None = None) -> str:
        return self.engine.upload(path, mime_type=mime_type)


def pick_provider(capability: str, provider_key: str | None = None) -> providers.Provider:
    """The provider that will serve this capability.

    * An explicit ``provider_key`` always wins.
    * Otherwise the first enabled provider for the capability is used — local
      ones before remote ones, because work done on this machine costs
      nothing, needs no key and never leaves the disk.
    * A forced engine (tests, or an explicit override) is honoured too.

    The product never mentions a vendor anywhere else; this is the seam.
    """
    if provider_key:
        return providers.pick(capability, provider_key)
    if _ENGINE is not None:
        return EngineProvider(_ENGINE)
    # The backends register themselves when the module is imported.
    from ops_dashboard import media_backends  # noqa: F401  (registration side effect)
    return providers.pick(capability)


def provider_catalogue() -> list[dict[str, Any]]:
    """Every provider and its state, for the settings panel."""
    from ops_dashboard import media_backends  # noqa: F401  (registration side effect)
    return providers.describe_all()


def _provider_for_job(job: dict[str, Any]) -> providers.Provider:
    """The provider that is rendering this job.

    A job remembers who accepted it: asking a different provider about a
    task it never saw would hang until the job was given up on.
    """
    if _ENGINE is not None:
        return EngineProvider(_ENGINE)
    key = (job.get("meta") or {}).get("provider") or job.get("engine")
    try:
        return pick_provider(providers.VIDEO, key)
    except Exception:
        return pick_provider(providers.VIDEO)


# ── ffmpeg: assembly, not generation ───────────────────────────────────────

def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def allowed_media_path(path: str, *, must_exist: bool = True,
                       scope: str = "media") -> str | None:
    """Resolve ``path`` only if it stays inside an allowed root.

    The endpoints take file paths, and an unchecked one would let a crafted
    request hand an arbitrary local file to ffmpeg, or upload it to the
    engine. Returns None instead of raising when the file is absent, so the
    caller can tell "not found" from "not allowed".

    ``scope="media"`` allows only the media library and is what anything
    leaving this Mac uses: the data directory also holds the database, and a
    keyframe should never be able to reach out with 73 MB of it.
    ``scope="data"`` is for local work (ffmpeg assembly), where the broader
    root is fine because nothing is transmitted.
    """
    try:
        rp = str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return None
    data_root = str(app_paths.data_dir().resolve())
    media_root = str((app_paths.data_dir() / "media").resolve())
    roots = [media_root] if scope == "media" else [media_root, data_root]
    if not any(rp == r or rp.startswith(r + os.sep) for r in roots):
        return None
    if must_exist and not os.path.isfile(rp):
        return None
    return rp


def probe(path: str | Path) -> dict[str, Any]:
    """Duration / size / resolution of a media file, via ffprobe."""
    path = str(path)
    if not os.path.isfile(path):
        raise MediaError(f"file not found: {path}")
    if not ffmpeg_available():
        raise MediaError("ffmpeg/ffprobe not installed")
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=60,
        ).stdout
        data = json.loads(out or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        raise MediaError(f"ffprobe failed: {e}") from e
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "duration_sec": round(float(fmt.get("duration") or 0), 2),
        "bytes": int(fmt.get("size") or 0),
        "width": video.get("width"),
        "height": video.get("height"),
        "codec": video.get("codec_name"),
        # H3 renders a native sound track; Hailuo is silent. Worth recording,
        # because the product promises "a clip with sound" only when it is true.
        "has_audio": bool(audio),
        "audio_codec": audio.get("codec_name") if audio else None,
        "audio_channels": audio.get("channels") if audio else None,
    }


def assemble(clips: list[str], out_path: str | Path, *, music: str | None = None,
             subtitles: str | None = None, workdir: str | None = None) -> str:
    """Cut clips into one video, optionally with a music bed and burned subtitles.

    ``clips`` are joined in order with the concat demuxer (stream copy, so it
    is fast and lossless). Music is mixed at 20% of its own volume and the
    video is re-encoded only when there is something to mix or burn.

    Only files under the product's own data directory are accepted: the
    endpoint takes a list of paths, and without that check any local file
    could be pulled into the output. 2026-10-23.
    """
    def _allowed(p: str) -> str:
        rp = allowed_media_path(p, scope="data")
        if rp is None:
            if not os.path.isfile(os.path.expanduser(str(p))):
                raise MediaError(f"file not found: {p}")
            raise MediaError(
                f"only files under the ConductLoop data directory can be used: {p}"
            )
        return rp

    clips = [_allowed(c) for c in (clips or []) if c]
    if not clips:
        raise MediaError("assemble needs at least one existing clip")
    music = _allowed(music) if music else None
    subtitles = _allowed(subtitles) if subtitles else None
    if not ffmpeg_available():
        raise MediaError("ffmpeg/ffprobe not installed")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(workdir or tempfile.mkdtemp(prefix="conductloop-media-"))

    list_file = tmp / "clips.txt"
    # The concat demuxer parses this file: a quote or a newline in a path
    # would inject another entry, so escape them (2026-10-23).
    list_file.write_text(
        "".join("file '" + c.replace("'", "'\\''") + "'\n" for c in clips),
        encoding="utf-8",
    )

    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file)]
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
    if subtitles:
        cmd += ["-vf", f"subtitles={subtitles}"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    if music:
        cmd += ["-c:a", "aac", "-filter_complex", "[1:a]volume=0.2[aout]", "-map", "0:v", "-map", "[aout]", "-shortest"]
    cmd += [str(out_path)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired as e:
        raise MediaError("ffmpeg timed out after 15 minutes") from e
    finally:
        if workdir is None:
            shutil.rmtree(tmp, ignore_errors=True)
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg failed: {(proc.stderr or '')[-400:]}")
    return str(out_path)


# ── prompt presets ─────────────────────────────────────────────────────────
#
# A bare sentence is a weak instruction. These are the shots that actually
# come up when you are running a product: an app icon, a landing hero, a
# thumbnail. Each one is a real prompt, not a style label, so the result is
# usable without editing. 2026-10-23.

@dataclass(frozen=True)
class ImagePreset:
    key: str
    label: str
    hint: str
    aspect_ratio: str
    prompt: str
    # The background is a choice, not an accident. Leaving it to the model
    # produced pastel, neon, white and even transparent icons for the same
    # preset — and a transparent icon is rejected by the App Store. 2026-10-23.
    backgrounds: tuple[str, ...] = ()


IMAGE_PRESETS: tuple[ImagePreset, ...] = (
    ImagePreset(
        "app_icon", "App icon",
        "Single centred mark, opaque background, no text",
        "1:1",
        "A single {subject} centred on a solid {background} background, "
        "the square tile filling the entire frame edge to edge with the same "
        "solid colour in all four corners, no circular badge, no inset panel, "
        "bold simple silhouette, soft depth, subtle inner glow, premium app "
        "icon, flat vector feel, no text, no letters, generous margin around "
        "the shape, high contrast at small sizes, fully opaque background, "
        "no transparency, no gradients",
        backgrounds=("deep midnight navy", "near-black charcoal",
                     "warm ivory", "deep forest green", "deep espresso brown"),
    ),
    ImagePreset(
        "landing_hero", "Landing hero",
        "Wide cinematic banner with room for a headline",
        "16:9",
        "Cinematic wide shot of {subject}, dramatic volumetric lighting, deep "
        "atmospheric depth, dark uncluttered background with empty space on the "
        "left for text, premium product photography, ultra sharp, subtle film "
        "grain, no text, no visible brand logos, no brand names, unbranded "
        "hardware, no trademarks",
    ),
    ImagePreset(
        "thumbnail", "Video thumbnail",
        "High contrast, readable at small size",
        "16:9",
        "Bold {subject} as the clear focal point, punchy saturated colours, "
        "strong rim light, dramatic contrast, slightly exaggerated perspective, "
        "designed to read at thumbnail size, no text, no visible brand logos",
    ),
    ImagePreset(
        "product", "Product photo",
        "Studio lighting on a neutral backdrop",
        "4:3",
        "Studio product photograph of {subject}, soft key light and subtle rim "
        "light, seamless neutral backdrop, accurate materials and reflections, "
        "crisp focus, commercial catalogue quality, no text, no visible brand logos",
    ),
    ImagePreset(
        "avatar", "Avatar",
        "Square portrait, friendly, neutral background",
        "1:1",
        "Friendly headshot portrait of {subject}, natural expression, soft "
        "directional light, clean out-of-focus background, shallow depth of "
        "field, photorealistic, centred composition, no text",
    ),
    ImagePreset(
        "abstract_bg", "Abstract background",
        "Ambient texture for a UI or a slide",
        "16:9",
        "Abstract generative background of {subject}, smooth flowing gradient "
        "forms, fine grain, deep and calm colours, no focal object, no text, "
        "suitable as a UI backdrop",
    ),
)


def prompt_presets() -> list[dict[str, Any]]:
    """The presets, ready for the client."""
    return [{"key": p.key, "label": p.label, "hint": p.hint,
             "aspect_ratio": p.aspect_ratio, "prompt": p.prompt,
             "backgrounds": list(p.backgrounds)}
            for p in IMAGE_PRESETS]


def apply_preset(key: str, subject: str, *, aspect_ratio: str | None = None,
                 background: str | None = None) -> dict[str, str]:
    """Expand a preset into a real prompt.

    ``subject`` is the only thing the caller supplies, so the preset stays
    editable: the result is a prompt you can keep tweaking, not a black box.
    """
    preset = next((p for p in IMAGE_PRESETS if p.key == key), None)
    if preset is None:
        raise MediaError(
            f"unknown preset '{key}'; try: {', '.join(p.key for p in IMAGE_PRESETS)}"
        )
    subject = (subject or "").strip()
    if not subject:
        raise MediaError("subject is required for a preset")
    if "{subject}" not in preset.prompt:
        # A preset that does not mention its subject would quietly ignore it,
        # which is how "make it blue" becomes an unrelated picture.
        raise MediaError(f"preset '{preset.key}' does not use the subject")
    chosen = (background or "").strip()
    if preset.backgrounds:
        if chosen and chosen not in preset.backgrounds:
            raise MediaError(
                f"background must be one of: {', '.join(preset.backgrounds)}"
            )
        chosen = chosen or preset.backgrounds[0]
    return {
        "prompt": preset.prompt.format(subject=subject, background=chosen or "solid"),
        "aspect_ratio": aspect_ratio or preset.aspect_ratio,
        "background": chosen,
    }


def mix_voiceover(video: str, voice: str, out_path: str | Path, *,
                  music: str | None = None, music_volume: float = 0.15,
                  fade: float = 0.5) -> str:
    """Put a spoken track under a clip, optionally over a music bed.

    The H3 family renders its own sound track, but it is the model this
    account cannot pay for. Hailuo is silent and cheap, so a narrated clip
    is built here: the voice is mixed under the picture and the video stream
    is copied untouched (no re-encode, so it stays as sharp as it was).

    Only files under the product's own data directory are accepted.
    """
    if not ffmpeg_available():
        raise MediaError("ffmpeg/ffprobe not installed")
    src = allowed_media_path(video)
    if not src:
        raise MediaError("the video must live inside the ConductLoop media library")
    voice_path = allowed_media_path(voice)
    if not voice_path:
        raise MediaError("the narration must live inside the ConductLoop media library")
    bed = allowed_media_path(music) if music else None
    if music and not bed:
        raise MediaError("the music bed must live inside the ConductLoop media library")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-i", src, "-i", voice_path]
    if bed:
        cmd += ["-stream_loop", "-1", "-i", bed]
    cmd += ["-map", "0:v", "-c:v", "copy"]
    if bed:
        # The voice stays at full level; the bed sits far below it.
        cmd += ["-map", "2:a", "-filter_complex",
                f"[1:a]volume=1.0,afade=t=in:d={fade}[v];"
                f"[2:a]volume={music_volume}[m];"
                f"[v][m]amix=inputs=2:duration=first:dropout_transition=2[a]",
                "-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    else:
        cmd += ["-map", "1:a", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += [str(out_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired as e:
        raise MediaError("ffmpeg timed out after 15 minutes") from e
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg failed: {(proc.stderr or '')[-400:]}")
    return str(out_path)


def voiceover(video_path: str, script: str, *, project_key: str | None = None,
             voice_id: str | None = None, emotion: str | None = None,
             speed: float | None = None, music: str | None = None,
             music_volume: float = 0.15,
             provider: str | None = None) -> MediaResult:
    """Give a silent clip a spoken narration.

    One call: the script is spoken, the audio is mixed under the picture and
    the result is recorded as a new asset. This is how a clip gets a voice
    without the H3 family, which is the model this account cannot pay for.
    2026-10-23.
    """
    from ops_dashboard import db_media

    if not (script or "").strip():
        raise MediaError("the narration script is required")
    video = allowed_media_path(video_path)
    if not video:
        raise MediaError("the video must live inside the ConductLoop media library")

    speech = synthesize_speech(script, project_key=project_key, voice_id=voice_id,
                                emotion=emotion, speed=speed, provider=provider)
    if not speech.path:
        raise MediaError("the narration could not be synthesized")
    out = media_dir(project_key) / _unique_name(f"{Path(video).stem}-narrated", ".mp4")
    mix_voiceover(video, speech.path, out, music=music, music_volume=music_volume)
    info = {}
    try:
        info = probe(out)
    except MediaError:
        info = {}
    asset_id = db_media.add_asset(
        kind="video", prompt=script, project_key=project_key, engine="ffmpeg",
        path=str(out), status="ready",
        meta={**info, "voiceover": True, "voice_id": voice_id or DEFAULT_VOICE_ID,
              "emotion": emotion, "speed": speed, "source_video": video,
              "narration": speech.path},
    )
    return MediaResult(ok=True, kind="video", path=str(out), meta={"asset_id": asset_id, **info})


# ── the product-level operations ───────────────────────────────────────────
def _validate_image(prompt: str, aspect_ratio: str, resolution: str, n: int,
                    image_path: str | None = None) -> None:
    """Input rules for image generation.

    These live at the product level, not inside the engine, so that EVERY
    engine (today the Matrix connector, tomorrow a direct MiniMax API call or
    an MCP server) is held to the same contract.
    """
    if not (prompt or "").strip():
        raise MediaError("prompt is required")
    if aspect_ratio not in ASPECT_RATIOS:
        raise MediaError(f"aspect_ratio must be one of: {', '.join(ASPECT_RATIOS)}")
    if resolution not in IMAGE_RESOLUTIONS:
        raise MediaError(f"resolution must be one of: {', '.join(IMAGE_RESOLUTIONS)}")
    if not (1 <= int(n or 0) <= 4):
        raise MediaError("n must be between 1 and 4")
    if image_path:
        ext = Path(image_path).suffix.lower()
        if ext not in KEYFRAME_MIME:
            raise MediaError(f"unsupported reference format '{ext or image_path}'")
        if not os.path.isfile(image_path):
            raise MediaError(f"reference image not found: {image_path}")


def _validate_video(prompt: str, model: str, duration: int,
                    image_path: str | None = None,
                    reference_type: str | None = None) -> None:
    """Input rules for video, held above the provider.

    Same reasoning as :func:`_validate_image`: the contract belongs to the
    product so a future provider cannot quietly accept a combination the
    provider actually bills for and then fails.
    """
    if not (prompt or "").strip():
        raise MediaError("prompt is required")
    MatrixVideoContract().validate_video(
        model=model, duration=int(duration or 0), reference_type=reference_type,
        has_image=bool(image_path),
    )
    if image_path:
        ext = Path(image_path).suffix.lower()
        if ext not in KEYFRAME_MIME:
            raise MediaError(f"unsupported keyframe format '{ext or image_path}'")
        if not os.path.isfile(os.path.expanduser(image_path)):
            raise MediaError(f"keyframe not found: {image_path}")


def video_capabilities() -> list[dict[str, Any]]:
    """What the UI needs to build a form that can only submit valid renders.

    These are the MiniMax models, because that is what this product can
    render today. The general version is :func:`provider_video_options`,
    which asks every provider what it offers.
    """
    return MatrixVideoContract().video_options()["models"]


def provider_video_options() -> list[dict[str, Any]]:
    """What each enabled provider can do for video.

    The UI builds its form from this, so a local ComfyUI workflow and the
    MiniMax models live in the same list without either of them being
    hardcoded in the page.
    """
    from ops_dashboard import media_backends  # noqa: F401  (registration)

    out: list[dict[str, Any]] = []
    for key in providers.enabled_keys(providers.VIDEO):
        try:
            provider = providers.build(key)
        except Exception:
            continue
        options = provider.video_options()
        models = options.get("models") or []
        if not models:
            models = [{"model": options.get("default_model") or key,
                       "durations": list(range(1, 61)),
                       "duration_range": [1, 60],
                       "resolutions": list(IMAGE_RESOLUTIONS),
                       "resolutions_by_duration": {},
                       "accepts_ratio": options.get("accepts_ratio", True),
                       "native_audio": False,
                       "keyframe_modes": list(options.get("keyframe_modes") or []),
                       "references": bool(options.get("references")),
                       "default": True, "fast": False}]
        for entry in models:
            out.append({**entry, "provider": key,
                        "provider_label": provider.spec.label})
    return out


def generate_image(prompt: str, *, project_key: str | None = None,
                   aspect_ratio: str = "1:1", resolution: str = "1K",
                   n: int = 1, image_path: str | None = None,
                   provider: str | None = None) -> MediaResult:
    """Generate an image and record it as an asset.

    ``image_path`` turns this into image-to-image: the render follows the
    reference's composition and palette instead of inventing one.

    ``provider`` names the provider to use. Left out, the product picks the
    enabled one — a local one first, because that work is free.
    """
    from ops_dashboard import db_media

    n = int(n or 1)
    image_path = str(Path(image_path).expanduser().resolve()) if image_path else None
    _validate_image(prompt, aspect_ratio, resolution, n, image_path)

    backend = pick_provider(providers.IMAGE, provider)
    with providers.counted(backend):
        res = backend.generate_image(prompt, aspect_ratio=aspect_ratio,
                                     resolution=resolution, project_key=project_key,
                                     n=n, image_path=image_path)
    if res.ok and res.path:
        info = {}
        try:
            info = probe(res.path) if res.path.lower().endswith((".mp4", ".mov", ".webm")) else {}
        except MediaError:
            info = {}
        res.meta.update(info)
        res.meta.setdefault("provider", backend.spec.key)
        # One asset row per generated file (n > 1 produces several).
        for saved in (res.meta.get("paths") or [res.path]):
            db_media.add_asset(
                kind="image", prompt=prompt, project_key=project_key,
                engine=backend.identity, path=saved, source_url=res.url,
                node_id=res.node_id, status="ready", meta=res.meta,
            )
    return res


def submit_video(prompt: str, *, project_key: str | None = None,
                 model: str | None = None, duration: int = 6,
                 ratio: str = "16:9", resolution: str | None = None,
                 image_path: str | None = None,
                 last_frame_path: str | None = None,
                 reference_type: str | None = None,
                 provider: str | None = None) -> dict[str, Any]:
    """Submit a video generation and open a job to track it.

    ``image_path`` animates a still: it is uploaded to the provider and used
    as the first frame. That is the whole image-to-video path.
    """
    from ops_dashboard import db_media

    duration = int(duration or 0)
    # Resolved once, here: every later check (validation, upload, the row we
    # store) then talks about the same real file instead of a path that only
    # made sense from the caller's working directory.
    image_path = str(Path(image_path).expanduser().resolve()) if image_path else None
    last_frame_path = str(Path(last_frame_path).expanduser().resolve()) if last_frame_path else None

    backend = pick_provider(providers.VIDEO, provider)
    model = model or backend.default_video_model()
    _validate_video(prompt, model, duration, image_path, reference_type)
    backend.validate_video(model=model, duration=duration, resolution=resolution,
                           ratio=ratio, reference_type=reference_type,
                           has_image=bool(image_path))
    if last_frame_path and not os.path.isfile(last_frame_path):
        raise MediaError(f"last frame not found: {last_frame_path}")
    # Make the keyframe mode explicit here rather than letting each provider
    # guess it, so every provider receives the same complete contract.
    reference_type = reference_type or ("first_frame" if image_path else None)

    with providers.counted(backend):
        res = backend.submit_video(
            prompt, model=model, duration=duration, ratio=ratio, resolution=resolution,
            image_path=image_path, last_frame_path=last_frame_path,
            reference_type=reference_type,
        )
    meta = dict(res.meta or {})
    if image_path:
        meta["keyframe"] = image_path
    meta["provider"] = backend.spec.key
    job_id = db_media.add_job(
        kind="video", prompt=prompt, project_key=project_key,
        engine=backend.identity, model=meta.get("model", model),
        external_task_id=res.task_id, status="queued", meta=meta,
    )
    return {"job_id": job_id, "task_id": res.task_id, "status": "queued",
            "model": meta.get("model", model),
            "provider": backend.spec.key,
            "native_audio": bool(meta.get("native_audio")),
            "fell_back": bool(meta.get("fell_back"))}


def animate_image(image_path: str, prompt: str, *, project_key: str | None = None,
                  model: str | None = None, duration: int = 6,
                  resolution: str | None = None,
                  provider: str | None = None) -> dict[str, Any]:
    """Turn a generated image into a clip: the product's one-call image-to-video.

    Without an explicit model the provider's own default is used, and the
    duration is corrected to one the model really accepts — an animation is
    usually a draft, so it should not fail over a duration.
    """
    backend = pick_provider(providers.VIDEO, provider)
    chosen = model or backend.draft_video_model()
    duration = int(duration or 0)
    if not duration:
        duration = 6
    try:
        backend.validate_video(model=chosen, duration=duration,
                               resolution=resolution, has_image=True)
    except MediaError:
        # The requested duration is not one this model takes: pick the first
        # one it does, and say so.
        options = backend.video_options()
        durations = []
        for entry in (options.get("models") or []):
            if entry.get("model") == chosen:
                durations = list(entry.get("durations") or [])
                break
        if durations:
            duration = durations[0]
        else:
            duration = duration or 6
    return submit_video(
        prompt, project_key=project_key, model=chosen, duration=duration,
        resolution=resolution, image_path=image_path, reference_type="first_frame",
        provider=provider,
    )


def poll_video(job_id: int) -> dict[str, Any]:
    """Poll a video job: download the file when it is ready, and store it."""
    from ops_dashboard import db_media

    job = db_media.get_job(int(job_id))
    if not job:
        raise MediaError(f"no media job with id {job_id}")
    if job["status"] in ("succeeded", "failed"):
        return job
    # The job remembers who is rendering it: the provider that accepted the
    # render is the one that has to be asked about it later.
    backend = _provider_for_job(job)
    res = backend.poll_video(job["external_task_id"], model=job["model"] or backend.default_video_model(),
                             project_key=job.get("project_key"))
    # The engine speaks several casings ("Success", "Processing", "success").
    # The product keeps one vocabulary: queued | running | succeeded | failed.
    status = (res.status or "running").strip().lower()
    if status in ("success", "succeeded", "completed", "done"):
        status = "succeeded"
    elif status in ("fail", "failed", "cancelled", "canceled", "error"):
        status = "failed"
    elif status in ("queued", "queueing", "preparing"):
        status = "queued"
    else:
        status = "running"

    if res.ok and status == "succeeded":
        info = {}
        try:
            info = probe(res.path) if res.path else {}
        except MediaError:
            info = {}
        asset_id = db_media.add_asset(
            kind="video", prompt=job.get("prompt"), project_key=job.get("project_key"),
            engine=job.get("engine"), model=job.get("model"), path=res.path,
            source_url=res.url, status="ready", meta={**info, **(res.meta or {})},
        )
        db_media.update_job(job_id, status="succeeded", asset_id=asset_id,
                            meta={**(job.get("meta") or {}), **(res.meta or {})})
    elif status in ("failed", "fail", "cancelled", "canceled"):
        db_media.update_job(job_id, status="failed", error=res.error or "video generation failed")
    else:
        db_media.update_job(job_id, status=status or "running")
    return db_media.get_job(int(job_id))


def synthesize_speech(text: str, *, project_key: str | None = None,
                      voice_id: str | None = None, emotion: str | None = None,
                      speed: float | None = None, pitch: int | None = None,
                      volume: float | None = None,
                      provider: str | None = None) -> MediaResult:
    """Speak some text with whichever provider is set up for speech."""
    from ops_dashboard import db_media

    backend = pick_provider(providers.SPEECH, provider)
    with providers.counted(backend):
        res = backend.synthesize_speech(
            text, project_key=project_key, voice_id=voice_id, emotion=emotion,
            speed=speed, pitch=pitch, volume=volume)
    if res.ok and res.path:
        res.meta.setdefault("provider", backend.spec.key)
        db_media.add_asset(
            kind="audio", prompt=text, project_key=project_key,
            engine=backend.identity, path=res.path, status="ready", meta=res.meta,
        )
    return res


# A job that never settles must not be polled forever: the engine forgets a
# task after a while, and a job stuck in "queued" would be re-polled on every
# scheduler tick for the life of the install. 2026-10-23.
MAX_JOB_ATTEMPTS = 8
MAX_JOB_AGE_SECONDS = 2 * 60 * 60


def reap_jobs(limit: int = 5) -> dict:
    """Poll the pending video jobs and finish the ones that are ready.

    Video generation is asynchronous: without somebody polling it, the
    render finished (and was paid for) but the file was never downloaded and
    the job stayed "running" forever. The scheduler calls this on every tick
    so the flow completes even if the UI was closed. 2026-10-23.

    A job that cannot be polled is counted and given up on, rather than
    retried silently for ever.
    """
    from ops_dashboard import db_media

    log = logging.getLogger(__name__)
    pending = db_media.list_jobs(status="queued", limit=limit) + \
        db_media.list_jobs(status="running", limit=limit)
    done = failed = stuck = 0
    now = int(time.time())
    for job in pending:
        meta = dict(job.get("meta") or {})
        attempts = int(meta.get("attempts") or 0) + 1
        meta["attempts"] = attempts
        age = now - int(job.get("created_at") or now)

        # Give up before asking again: an expired task_id never comes back,
        # and neither does one whose file can no longer be downloaded.
        if attempts >= MAX_JOB_ATTEMPTS or age >= MAX_JOB_AGE_SECONDS:
            reason = meta.get("last_error") or (
                f"gave up after {attempts} polls over {age // 60} min")
            log.warning("media job %s stalled: %s", job.get("id"), reason)
            db_media.update_job(int(job["id"]), status="failed",
                                error=f"render did not complete: {reason}",
                                meta=meta)
            stuck += 1
            continue

        try:
            updated = poll_video(int(job["id"]))
        except Exception as exc:            # noqa: BLE001 - one bad job must
            meta["last_error"] = str(exc)[:300]   # not stop the others
            db_media.update_job(int(job["id"]), meta=meta)
            log.warning("media job %s poll failed: %s", job.get("id"), exc)
            continue
        status = (updated or {}).get("status")
        if status == "succeeded":
            done += 1
        elif status == "failed":
            failed += 1
    return {"checked": len(pending), "succeeded": done, "failed": failed,
            "given_up": stuck}


def engine_status() -> dict[str, Any]:
    """What the product shows in its settings: is media generation usable?"""
    eng = get_engine()
    catalogue = provider_catalogue()
    return {
        "engine": eng.name,
        "available": eng.available(),
        "cli": getattr(eng, "_cli", None),
        "ffmpeg": ffmpeg_available(),
        "video_models": list(VIDEO_MODELS),
        "default_video_model": DEFAULT_VIDEO_MODEL,
        "fast_video_model": FAST_VIDEO_MODEL,
        "fallback_video_model": FALLBACK_VIDEO_MODEL,
        "video_capabilities": video_capabilities(),
        "provider_video_options": provider_video_options(),
        "providers": catalogue,
        "capabilities": providers.capability_map(),
        "keyframe_modes": list(KEYFRAME_MODES),
        "keyframe_formats": sorted(KEYFRAME_MIME),
        "aspect_ratios": list(ASPECT_RATIOS),
        "image_resolutions": list(IMAGE_RESOLUTIONS),
        "image_presets": prompt_presets(),
        "checked_at": int(time.time()),
    }
