"""The backends: one class per vendor or local tool.

Each class implements only the capabilities it declares in its
:class:`~ops_dashboard.media_providers.ProviderSpec`. They all speak the
same small contract, so the product can swap one for another without
changing a line of logic:

* ``generate_image(prompt, …)`` → a local file
* ``submit_video(prompt, …)`` / ``poll_video(task_id, …)`` → a task, then a file
* ``synthesize_speech(text, …)`` → a local file
* ``embed(texts)`` → vectors
* ``chat(messages)`` → text
* ``health()`` → is it usable right now

What is here
------------
``matrix``            the MiniMax connector (was "the engine")
``openai``            anything that speaks the OpenAI API — OpenAI itself,
                      DeepSeek, Together, Groq, LM Studio, llama.cpp's server,
                      vLLM, Ollama's OpenAI-compatible endpoint. One class,
                      every model.
``ollama``            Ollama's own API: text and embeddings
``comfyui``           ComfyUI on this Mac: images and video, fully local
``macos_say``         the speech synthesiser that ships with macOS: no
                      dependency, no network, works offline
``piper``             Piper (or any piper-server): local TTS with real voices

Every one of them is optional. The product picks the first enabled provider
that can serve the capability, and says clearly when none can.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

from . import media
from . import media_providers as mp
from .media_providers import MediaError, MediaResult, Provider, ProviderSpec

# ── shared helpers ─────────────────────────────────────────────────────────

def _write_bytes(data: bytes, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def _unique(dest: Path) -> Path:
    """A name that does not collide with an earlier run."""
    stem, suffix = dest.stem, dest.suffix
    return dest.with_name(f"{stem}-{int(time.time())}-{os.getpid()}{suffix}")


# ── MiniMax / Matrix (the original engine) ─────────────────────────────────

class MatrixProvider(media.MatrixVideoContract, Provider):
    """The MiniMax connector, reached through the mcode-tools CLI.

    It stays as one provider among others: images, video and speech, with
    its own quirks (a 402 on the H3 family, a fallback model, upload before
    image-to-video).

    The transport lives in :class:`~ops_dashboard.media.MatrixEngine`, which
    already carries the hard-won details (the credit refusal, the ratio
    rules, the upload before an image-to-video). This class only presents
    that engine through the provider contract, so there is one
    implementation of the MiniMax quirks and not two.
    """

    def _engine(self):
        from . import media

        return media.MatrixEngine()

    def configured(self) -> bool:
        return bool(self._engine().available())

    def health(self) -> tuple[bool, str]:
        try:
            if self._engine().available():
                return True, "the MiniMax connector is reachable"
            return False, "the mcode-tools CLI was not found"
        except Exception as e:
            return False, str(e)[:200]

    # ── capabilities ─────────────────────────────────────────────────────

    def generate_image(self, prompt: str, **kw) -> MediaResult:
        return self._engine().generate_image(prompt, **kw)

    def submit_video(self, prompt: str, **kw) -> MediaResult:
        return self._engine().submit_video(prompt, **kw)

    def poll_video(self, task_id: str, **kw) -> MediaResult:
        return self._engine().poll_video(task_id, **kw)

    def synthesize_speech(self, text: str, **kw) -> MediaResult:
        return self._engine().synthesize_speech(text, **kw)

    def upload(self, path: str, *, mime_type: str | None = None) -> str:
        return self._engine().upload(path, mime_type=mime_type)


# ── OpenAI-compatible: one class, every model ──────────────────────────────

class OpenAIProvider(Provider):
    """Anything that speaks the OpenAI API.

    That is a much wider family than OpenAI itself: DeepSeek, Together,
    Groq, Fireworks, Mistral's own endpoint, and the local servers —
    LM Studio, llama.cpp's ``--server``, vLLM, Ollama's
    ``/v1`` compatibility layer. One class, one contract, any model.

    The base url decides which one you are talking to.
    """

    def _headers(self) -> dict[str, str]:
        head = {"Content-Type": "application/json"}
        if self.api_key:
            head["Authorization"] = f"Bearer {self.api_key}"
        return head

    def chat(self, messages: list[dict], *, model: str | None = None,
             temperature: float | None = None, max_tokens: int | None = None,
             **_) -> str:
        if not self.base_url:
            raise MediaError("the OpenAI-compatible provider needs a base url")
        payload: dict[str, Any] = {
            "model": model or self.get("model", "gpt-4o-mini"),
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        data = mp.http_json(f"{self.base_url}/chat/completions",
                             method="POST", headers=self._headers(), payload=payload)
        choices = data.get("choices") or []
        if not choices:
            raise MediaError(f"the model answered with no choices: {str(data)[:200]}")
        return (choices[0].get("message") or {}).get("content") or ""

    def embed(self, texts: list[str], *, model: str | None = None, **_) -> list[list[float]]:
        if not self.base_url:
            raise MediaError("the OpenAI-compatible provider needs a base url")
        payload = {"model": model or self.get("embed_model", "text-embedding-3-small"),
                   "input": list(texts)}
        data = mp.http_json(f"{self.base_url}/embeddings",
                             method="POST", headers=self._headers(), payload=payload)
        items = data.get("data") or []
        if not items:
            raise MediaError(f"the model returned no embeddings: {str(data)[:200]}")
        return [it.get("embedding") or [] for it in items]

    def generate_image(self, prompt: str, *, aspect_ratio: str = "1:1",
                       resolution: str = "1K", project_key: str | None = None,
                       n: int = 1, image_path: str | None = None, **_) -> MediaResult:
        """The /v1/images/generations endpoint (gpt-image-1 and friends).

        It answers with base64, so there is no URL to download: the bytes
        are decoded and written straight to the library.
        """
        from . import media

        if not self.base_url:
            raise MediaError("the OpenAI-compatible provider needs a base url")
        size = {"1K": "1024x1024", "2K": "1536x1024", "4K": "1536x1024"}.get(
            resolution, "1024x1024")
        if aspect_ratio in ("9:16", "3:4"):
            size = "1024x1536"
        payload = {"model": self.get("image_model", "gpt-image-1"),
                   "prompt": prompt, "n": max(1, min(int(n or 1), 4)), "size": size}
        data = mp.http_json(f"{self.base_url}/images/generations",
                             method="POST", headers=self._headers(), payload=payload)
        items = data.get("data") or []
        if not items:
            raise MediaError(f"the model returned no images: {str(data)[:200]}")
        paths: list[str] = []
        path = None
        for item in items:
            blob = item.get("b64_json")
            if not blob and item.get("url"):
                blob = mp.http_bytes(item["url"]).decode("latin-1")
                dest = media.media_dir(project_key) / media._unique_name(prompt, ".png")
                path = path or str(dest)
                paths.append(str(dest))
                continue
            if not blob:
                continue
            import base64

            dest = media.media_dir(project_key) / media._unique_name(prompt, ".png")
            _write_bytes(base64.b64decode(blob), dest)
            paths.append(str(dest))
            path = path or str(dest)
        if not path:
            raise MediaError("the model returned images with no data")
        return MediaResult(ok=True, kind="image", path=path, provider=self.spec.key,
                           meta={"items": len(items), "paths": paths, "saved": len(paths),
                                 "aspect_ratio": aspect_ratio, "resolution": resolution,
                                 "variation": bool(image_path)})

    def synthesize_speech(self, text: str, *, voice_id: str | None = None,
                          project_key: str | None = None, **_) -> MediaResult:
        from . import media

        if not self.base_url:
            raise MediaError("the OpenAI-compatible provider needs a base url")
        payload = {"model": self.get("tts_model", "gpt-4o-mini-tts"),
                   "voice": voice_id or self.get("voice", "alloy"), "input": text}
        data = mp.http_bytes(f"{self.base_url}/audio/speech", method="POST",
                             headers=self._headers(), payload=payload)
        if not data:
            raise MediaError("the model returned no audio")
        dest = media.media_dir(project_key) / media._unique_name(text[:40], ".mp3")
        _write_bytes(data, dest)
        return MediaResult(ok=True, kind="audio", path=str(dest), provider=self.spec.key,
                           meta={"voice_id": voice_id, "bytes": len(data)})

    def health(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, "no base url configured"
        try:
            mp.http_json(f"{self.base_url}/models", headers=self._headers(), timeout=20)
            return True, f"{self.base_url} is answering"
        except Exception as e:
            return False, str(e)[:200]


# ── Ollama ─────────────────────────────────────────────────────────────────

class OllamaProvider(Provider):
    """Ollama's own API on this Mac.

    Text and embeddings, no key, no cost, no network. Ollama also serves an
    OpenAI-compatible endpoint, but its native API is simpler for these two.
    """

    def _base(self) -> str:
        return self.base_url or "http://127.0.0.1:11434"

    def chat(self, messages: list[dict], *, model: str | None = None, **_) -> str:
        payload = {"model": model or self.get("model", "llama3.2"),
                   "messages": messages, "stream": False}
        data = mp.http_json(f"{self._base()}/api/chat", method="POST", payload=payload,
                             timeout=300)
        message = data.get("message") or {}
        if not message.get("content"):
            raise MediaError(f"Ollama answered with no content: {str(data)[:200]}")
        return message["content"]

    def embed(self, texts: list[str], *, model: str | None = None, **_) -> list[list[float]]:
        name = model or self.get("embed_model", "nomic-embed-text")
        out = []
        for text in texts:
            data = mp.http_json(f"{self._base()}/api/embeddings", method="POST",
                                 payload={"model": name, "prompt": text}, timeout=120)
            vec = data.get("embedding")
            if not vec:
                raise MediaError(f"Ollama returned no embedding: {str(data)[:200]}")
            out.append(vec)
        return out

    def health(self) -> tuple[bool, str]:
        try:
            data = mp.http_json(f"{self._base()}/api/tags", timeout=8)
            models = [m.get("name") for m in (data.get("models") or [])]
            return True, f"{len(models)} model(s) installed"
        except Exception as e:
            return False, str(e)[:200]


# ── ComfyUI ────────────────────────────────────────────────────────────────

class ComfyProvider(Provider):
    """ComfyUI running on this Mac: images and video, entirely local.

    It is driven with its own API: a workflow (a JSON graph) is queued and
    the result is polled. The workflow is configuration, so a user can point
    it at whatever checkpoint and sampler they have.

    The image workflow is expected to have one node that takes the prompt
    and one that saves the output; the node ids are configurable because
    every workflow names them differently. A video workflow can be given
    separately: image and video graphs are different graphs.
    """

    def _base(self) -> str:
        return self.base_url or "http://127.0.0.1:8188"

    def _workflow(self, which: str = "workflow") -> dict:
        raw = self.get(which, "")
        if not raw:
            raise MediaError(
                "the ComfyUI provider needs a workflow: paste the workflow "
                "JSON (API format) in its settings"
            )
        try:
            return json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError as e:
            raise MediaError(f"the ComfyUI workflow is not valid JSON: {e}") from e

    # ── workflow plumbing ─────────────────────────────────────────────────

    def _inject(self, workflow: dict, *, prompt: str, negative: str,
                prompt_node: str, negative_node: str,
                width: int | None = None, height: int | None = None) -> dict:
        """Write the prompt (and the size) into the workflow's nodes."""
        for node_id, node in (workflow.items() if isinstance(workflow, dict) else []):
            inputs = node.get("inputs") if isinstance(node, dict) else None
            if not isinstance(inputs, dict):
                continue
            if node_id == prompt_node:
                inputs["text"] = prompt
            elif node_id == negative_node:
                inputs["text"] = negative
            elif "width" in inputs and "height" in inputs:
                if width and height:
                    inputs["width"] = width
                    inputs["height"] = height
        return workflow

    def _queue(self, workflow: dict) -> str:
        data = mp.http_json(f"{self._base()}/prompt", method="POST",
                             payload={"prompt": workflow}, timeout=60)
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise MediaError(f"ComfyUI did not queue the job: {str(data)[:200]}")
        return str(prompt_id)

    def _history(self, prompt_id: str) -> dict:
        data = mp.http_json(f"{self._base()}/history/{prompt_id}", timeout=20)
        return data.get(prompt_id) or {}

    def _outputs(self, entry: dict) -> list[dict]:
        """Every file the finished run produced, images and videos alike."""
        found: list[dict] = []
        for node_out in (entry.get("outputs") or {}).values():
            for bucket in ("images", "videos", "gifs", "audio"):
                for item in node_out.get(bucket) or []:
                    if item.get("filename"):
                        found.append(item)
        return found

    def _fetch(self, item: dict, dest: Path) -> Path:
        query = urllib.parse.urlencode({
            "filename": item.get("filename", ""),
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        blob = mp.http_bytes(f"{self._base()}/view?{query}")
        return _write_bytes(blob, dest)

    # ── capabilities ─────────────────────────────────────────────────────

    def generate_image(self, prompt: str, *, aspect_ratio: str = "1:1",
                       resolution: str = "1K", project_key: str | None = None,
                       n: int = 1, image_path: str | None = None, **_) -> MediaResult:
        from . import media

        workflow = self._inject(
            self._workflow("workflow"), prompt=prompt,
            negative=self.get("negative_prompt", ""),
            prompt_node=str(self.get("prompt_node", "6")),
            negative_node=str(self.get("negative_node", "7")),
            width=_dimensions(aspect_ratio, resolution)[0],
            height=_dimensions(aspect_ratio, resolution)[1],
        )
        prompt_id = self._queue(workflow)
        entry = self._await(prompt_id)
        outputs = self._outputs(entry)
        if not outputs:
            raise MediaError("ComfyUI produced no image")
        paths: list[str] = []
        for item in outputs[: max(1, min(int(n or 1), 4))]:
            dest = media.media_dir(project_key) / media._unique_name(prompt, ".png")
            paths.append(str(self._fetch(item, dest)))
        return MediaResult(ok=True, kind="image", path=paths[0], provider=self.spec.key,
                           meta={"items": len(paths), "paths": paths, "saved": len(paths),
                                 "aspect_ratio": aspect_ratio, "resolution": resolution,
                                 "prompt_id": prompt_id, "variation": bool(image_path)})

    def submit_video(self, prompt: str, *, model: str | None = None,
                     duration: int = 6, ratio: str = "16:9",
                     resolution: str | None = None,
                     image_path: str | None = None,
                     last_frame_path: str | None = None,
                     reference_type: str | None = None, **_) -> MediaResult:
        """Queue a video workflow.

        ComfyUI has no fixed model names — the model is whatever checkpoint
        the workflow loads — so the "model" of a Comfy render is the
        workflow's own name.
        """
        workflow = self._inject(
            self._workflow("video_workflow"), prompt=prompt,
            negative=self.get("negative_prompt", ""),
            prompt_node=str(self.get("video_prompt_node", self.get("prompt_node", "6"))),
            negative_node=str(self.get("video_negative_node",
                                       self.get("negative_node", "7"))),
        )
        prompt_id = self._queue(workflow)
        return MediaResult(ok=True, kind="video", task_id=prompt_id, status="queued",
                           provider=self.spec.key,
                           meta={"model": model or "comfy", "duration": int(duration),
                                 "prompt_id": prompt_id})

    def poll_video(self, task_id: str, *, model: str | None = None,
                   project_key: str | None = None, **_) -> MediaResult:
        from . import media

        entry = self._history(str(task_id))
        if not entry:
            return MediaResult(ok=True, kind="video", task_id=str(task_id),
                               status="running", provider=self.spec.key,
                               meta={"model": model})
        outputs = self._outputs(entry)
        videos = [o for o in outputs
                  if str(o.get("filename", "")).lower().endswith(
                      (".mp4", ".webm", ".mov", ".gif"))]
        if videos:
            dest = media.media_dir(project_key) / media._unique_name(str(task_id), ".mp4")
            path = self._fetch(videos[0], dest)
            return MediaResult(ok=True, kind="video", path=str(path),
                               task_id=str(task_id), status="succeeded",
                               provider=self.spec.key,
                               meta={"model": model, "prompt_id": str(task_id)})
        if outputs:
            # It finished, but with something that is not a video.
            dest = media.media_dir(project_key) / media._unique_name(str(task_id), ".mp4")
            path = self._fetch(outputs[0], dest)
            return MediaResult(ok=True, kind="video", path=str(path),
                               task_id=str(task_id), status="succeeded",
                               provider=self.spec.key,
                               meta={"model": model, "prompt_id": str(task_id),
                                     "unexpected_format": True})
        status = (entry.get("status") or {}).get("status_str") or "running"
        return MediaResult(ok=True, kind="video", task_id=str(task_id),
                           status="failed" if status in ("error", "failed") else "running",
                           provider=self.spec.key, meta={"model": model})

    def _await(self, prompt_id: str, *, timeout: float = 300.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            entry = self._history(prompt_id)
            if entry and self._outputs(entry):
                return entry
            if entry and (entry.get("status") or {}).get("status_str") in ("error", "failed"):
                raise MediaError("ComfyUI reported a failed run")
            time.sleep(2)
        raise MediaError("ComfyUI did not finish in time")

    def health(self) -> tuple[bool, str]:
        try:
            data = mp.http_json(f"{self._base()}/system_stats", timeout=8)
            devices = data.get("devices") or []
            return True, f"ComfyUI is running ({len(devices)} device(s))"
        except Exception as e:
            return False, str(e)[:200]


def _dimensions(aspect_ratio: str, resolution: str) -> tuple[int, int]:
    """Pixels for a ratio and a resolution label."""
    long_side = {"1K": 1024, "2K": 2048, "4K": 4096}.get(resolution, 1024)
    ratios = {"1:1": (1, 1), "16:9": (16, 9), "9:16": (9, 16), "4:3": (4, 3),
              "3:4": (3, 4), "21:9": (21, 9)}
    rw, rh = ratios.get(aspect_ratio, (1, 1))
    if rw >= rh:
        return long_side, max(64, int(long_side * rh / rw) // 8 * 8)
    return max(64, int(long_side * rw / rh) // 8 * 8), long_side


# ── macOS say ──────────────────────────────────────────────────────────────

class MacSayProvider(Provider):
    """The speech synthesiser that ships with macOS.

    No dependency, no network, no key: it works with the machine off the
    internet. The quality is not the best, but a clip with a voice beats a
    silent one, and the alternative (a cloud TTS) costs money.
    """

    def synthesize_speech(self, text: str, *, voice_id: str | None = None,
                          project_key: str | None = None, **_) -> MediaResult:
        from . import media

        if not (text or "").strip():
            raise MediaError("text is required")
        if not shutil.which("say"):
            raise MediaError("the macOS speech synthesiser is not available")
        voice = voice_id or self.get("voice", "Samantha")
        tmp = tempfile.mkdtemp(prefix="conductloop-say-")
        aiff = os.path.join(tmp, "speech.aiff")
        try:
            proc = subprocess.run(["say", "-v", voice, "-o", aiff, text],
                                  capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                raise MediaError(f"say failed: {(proc.stderr or '').strip()[:200]}")
            if not os.path.isfile(aiff):
                raise MediaError("say produced no audio")
            dest = media.media_dir(project_key) / media._unique_name(text[:40], ".m4a")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if shutil.which("ffmpeg"):
                conv = subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-i", aiff, "-c:a", "aac", str(dest)],
                    capture_output=True, text=True, timeout=120)
                if conv.returncode != 0 or not dest.exists():
                    shutil.copy2(aiff, dest)
            else:
                shutil.copy2(aiff, dest)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return MediaResult(ok=True, kind="audio", path=str(dest), provider=self.spec.key,
                           meta={"voice_id": voice, "bytes": dest.stat().st_size})

    def health(self) -> tuple[bool, str]:
        if shutil.which("say"):
            return True, "the macOS speech synthesiser is available"
        return False, "say is not on PATH"


# ── Piper ──────────────────────────────────────────────────────────────────

class PiperProvider(Provider):
    """Piper, as a local HTTP service (piper-server) or as a binary.

    Piper is a small, fast, local text-to-speech: a handful of megabytes
    per voice and no GPU. Point the base url at a piper-server, or set the
    binary path to the CLI.
    """

    def synthesize_speech(self, text: str, *, voice_id: str | None = None,
                          project_key: str | None = None, **_) -> MediaResult:
        from . import media

        if not (text or "").strip():
            raise MediaError("text is required")
        dest = media.media_dir(project_key) / media._unique_name(text[:40], ".wav")
        base = self.base_url
        if base:
            data = mp.http_bytes(f"{base}/", method="POST",
                                 headers={"Content-Type": "text/plain"},
                                 payload=text.encode("utf-8"))
            _write_bytes(data, dest)
        else:
            binary = self.get("binary", "piper")
            model = self.get("voice_model", "")
            if not model:
                raise MediaError("the Piper provider needs a base url or a voice model")
            proc = subprocess.run([binary, "--model", model, "--output_file", str(dest)],
                                  input=text.encode("utf-8"), capture_output=True, timeout=300)
            if proc.returncode != 0 or not dest.exists():
                raise MediaError(f"piper failed: {(proc.stderr or '').strip()[:200]}")
        return MediaResult(ok=True, kind="audio", path=str(dest), provider=self.spec.key,
                           meta={"voice_id": voice_id, "bytes": dest.stat().st_size})

    def health(self) -> tuple[bool, str]:
        if self.base_url:
            try:
                mp.http_json(f"{self.base_url}/", timeout=8)
                return True, f"piper is answering at {self.base_url}"
            except Exception:
                pass
        if shutil.which(str(self.get("binary", "piper"))):
            return True, "the piper binary is on PATH"
        return False, "neither a piper server nor the piper binary was found"


# ── remote image APIs ──────────────────────────────────────────────────────
#
# Three services with the same shape: a prompt goes in, an image comes out.
# They differ in how the answer is delivered (bytes straight back, a url, or
# a job to poll), so each one is its own small class — but all of them are
# reached through the same product call.

class StabilityProvider(Provider):
    """Stability AI's image API.

    The answer is the image itself, as bytes: there is no url to follow and
    no job to poll.
    """

    ENDPOINTS = {
        "core": "/v2beta/stable-image/generate/core",
        "sd3": "/v2beta/stable-image/generate/sd3",
        "ultra": "/v2beta/stable-image/generate/ultra",
    }

    def _endpoint(self) -> str:
        model = (self.get("model", "core") or "core").lower()
        return self.base_url + self.ENDPOINTS.get(model, self.ENDPOINTS["core"])

    def _headers(self) -> dict[str, str]:
        head = {"Accept": "image/*"}
        if self.api_key:
            head["Authorization"] = f"Bearer {self.api_key}"
        return head

    def generate_image(self, prompt: str, *, aspect_ratio: str = "1:1",
                       resolution: str = "1K", project_key: str | None = None,
                       n: int = 1, image_path: str | None = None, **_) -> MediaResult:
        from . import media

        if not (prompt or "").strip():
            raise MediaError("prompt is required")
        # Stability takes a pixel size, not a ratio label.
        width, height = _dimensions(aspect_ratio, resolution)
        fields = {"prompt": prompt, "output_format": "png",
                  "width": str(width), "height": str(height)}
        if self.get("negative_prompt"):
            fields["negative_prompt"] = self.get("negative_prompt")
        body = _multipart(fields, self.get("image_path_seed") or "")
        data = mp.http_bytes(self._endpoint(), method="POST",
                             headers=self._headers(), payload=body)
        if not data:
            raise MediaError("Stability returned no image")
        dest = media.media_dir(project_key) / media._unique_name(prompt, ".png")
        _write_bytes(data, dest)
        return MediaResult(ok=True, kind="image", path=str(dest), provider=self.spec.key,
                           meta={"items": 1, "paths": [str(dest)], "saved": 1,
                                 "aspect_ratio": aspect_ratio, "resolution": resolution})

    def health(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "no API key configured"
        if not self.base_url:
            return False, "no base url configured"
        return True, f"{self.get('model', 'core')} is ready at {self.base_url}"


class ReplicateProvider(Provider):
    """Replicate: any model, as a prediction to poll.

    The model is named as ``owner/name``; a version can be pinned so the
    same model gives the same result tomorrow.
    """

    def _model_path(self) -> str:
        model = (self.get("model", "") or "").strip()
        if not model:
            raise MediaError("the Replicate provider needs a model (owner/name)")
        return model.strip("/")

    def _headers(self) -> dict[str, str]:
        head = {"Content-Type": "application/json"}
        if self.api_key:
            head["Authorization"] = f"Bearer {self.api_key}"
        return head

    def generate_image(self, prompt: str, *, aspect_ratio: str = "1:1",
                       resolution: str = "1K", project_key: str | None = None,
                       n: int = 1, image_path: str | None = None, **_) -> MediaResult:
        from . import media

        if not (prompt or "").strip():
            raise MediaError("prompt is required")
        width, height = _dimensions(aspect_ratio, resolution)
        payload: dict[str, Any] = {
            "input": {"prompt": prompt, "width": width, "height": height,
                      "num_outputs": max(1, min(int(n or 1), 4))},
        }
        version = self.get("version", "")
        if version:
            payload["version"] = version
        data = mp.http_json(f"{self.base_url}/v1/predictions", method="POST",
                             headers=self._headers(), payload=payload, timeout=120)
        prediction_id = data.get("id")
        if not prediction_id:
            raise MediaError(f"Replicate did not return a prediction: {str(data)[:200]}")
        output = self._await(prediction_id)
        urls = output if isinstance(output, list) else [output]
        paths: list[str] = []
        for url in urls:
            if not url:
                continue
            blob = mp.http_bytes(str(url))
            dest = media.media_dir(project_key) / media._unique_name(prompt, ".png")
            paths.append(str(_write_bytes(blob, dest)))
        if not paths:
            raise MediaError("Replicate produced no image")
        return MediaResult(ok=True, kind="image", path=paths[0], provider=self.spec.key,
                           meta={"items": len(paths), "paths": paths, "saved": len(paths),
                                 "aspect_ratio": aspect_ratio, "resolution": resolution,
                                 "prediction_id": prediction_id})

    def _await(self, prediction_id: str, *, timeout: float = 600.0) -> Any:
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = mp.http_json(f"{self.base_url}/v1/predictions/{prediction_id}",
                                 headers=self._headers(), timeout=60)
            status = data.get("status")
            if status == "succeeded":
                return data.get("output")
            if status in ("failed", "canceled"):
                raise MediaError(str(data.get("error") or "the Replicate prediction failed")[:300])
            time.sleep(2)
        raise MediaError("Replicate did not finish in time")

    def health(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "no API key configured"
        if not self.get("model"):
            return False, "no model configured"
        return True, f"{self.get('model')} is ready"


class FalProvider(Provider):
    """fal.ai: a model endpoint that answers with a url.

    The model is named the way fal names it (``fal-ai/flux/dev``), which is
    also the path of the endpoint.
    """

    def _headers(self) -> dict[str, str]:
        head = {"Content-Type": "application/json"}
        if self.api_key:
            head["Authorization"] = f"Key {self.api_key}"
        return head

    def generate_image(self, prompt: str, *, aspect_ratio: str = "1:1",
                       resolution: str = "1K", project_key: str | None = None,
                       n: int = 1, image_path: str | None = None, **_) -> MediaResult:
        from . import media

        if not (prompt or "").strip():
            raise MediaError("prompt is required")
        model = (self.get("model", "") or "").strip("/")
        if not model:
            raise MediaError("the fal provider needs a model (fal-ai/...)")
        width, height = _dimensions(aspect_ratio, resolution)
        payload = {"prompt": prompt, "image_size": {"width": width, "height": height},
                   "num_images": max(1, min(int(n or 1), 4))}
        data = mp.http_json(f"{self.base_url}/{model}", method="POST",
                             headers=self._headers(), payload=payload, timeout=300)
        urls = [item.get("url") for item in (data.get("images") or [])
                if isinstance(item, dict) and item.get("url")]
        if not urls:
            images = data.get("image") or {}
            if isinstance(images, dict) and images.get("url"):
                urls = [images["url"]]
        if not urls:
            raise MediaError(f"fal returned no image: {str(data)[:200]}")
        paths: list[str] = []
        for url in urls:
            blob = mp.http_bytes(str(url))
            dest = media.media_dir(project_key) / media._unique_name(prompt, ".png")
            paths.append(str(_write_bytes(blob, dest)))
        return MediaResult(ok=True, kind="image", path=paths[0], provider=self.spec.key,
                           meta={"items": len(paths), "paths": paths, "saved": len(paths),
                                 "aspect_ratio": aspect_ratio, "resolution": resolution})

    def health(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "no API key configured"
        if not self.get("model"):
            return False, "no model configured"
        return True, f"{self.get('model')} is ready"


def _multipart(fields: dict[str, str], file_field: str = "") -> bytes:
    """A multipart/form-data body, built by hand to avoid a dependency."""
    boundary = "----conductloop" + str(int(time.time() * 1000))
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    if file_field:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="image"; filename="image.png"\r\n'
            f"Content-Type: image/png\r\n\r\n".encode())
        parts.append(file_field.encode("latin-1", "ignore"))
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


# ── local embeddings ───────────────────────────────────────────────────────

class LocalEmbedProvider(Provider):
    """Embeddings computed on this machine, with ONNX.

    ``sentence-transformers`` needs torch, which is hundreds of megabytes —
    too much to ship inside the desktop app. ONNX Runtime runs the *same*
    model (all-MiniLM-L6-v2, 384 dimensions) in a fraction of that space,
    so the packaged app can have real semantic search instead of falling
    back to keyword stubs.

    The vectors are the same ones torch would produce, so an index built in
    development is readable by the packaged app: the model tag is the same
    on both sides.
    """

    def _embedder(self):
        cached = getattr(self, "_model", None)
        if cached is not None:
            return cached
        try:
            from fastembed import TextEmbedding
        except Exception as e:
            raise MediaError(
                "the local embedding model needs fastembed and onnxruntime; "
                f"they are not installed ({e})"
            ) from e
        from . import app_paths

        cache = str(app_paths.path("models", "data/models"))
        os.makedirs(cache, exist_ok=True)
        name = self.get("model", "sentence-transformers/all-MiniLM-L6-v2")
        self._model = TextEmbedding(model_name=name, cache_dir=cache)
        return self._model

    def embed(self, texts: list[str], **kw) -> list[list[float]]:
        model = self._embedder()
        out: list[list[float]] = []
        for vector in model.embed(list(texts)):
            values = [float(x) for x in vector]
            # The search compares vectors with a dot product, which only
            # equals cosine similarity when they are normalised.
            norm = sum(x * x for x in values) ** 0.5
            out.append([x / norm for x in values] if norm else values)
        return out

    def embedding_tag(self) -> str:
        """The tag stored with the vectors: the model, not the provider.

        It is the model on purpose: the same model computed with ONNX and
        with torch gives the same vectors, so an index built in development
        is readable by the packaged app. The "sentence-transformers/" prefix
        is dropped so both spellings of the same model share one tag.
        """
        name = self.get("model", "sentence-transformers/all-MiniLM-L6-v2")
        return name.split("/")[-1] if name.startswith("sentence-transformers/") else name

    def health(self) -> tuple[bool, str]:
        try:
            self._embedder()
            return True, "the local embedding model is ready"
        except Exception as e:
            return False, str(e)[:200]


# ── registration ───────────────────────────────────────────────────────────

def _field(key: str, label: str, kind: str = "text", placeholder: str = "",
           secret: bool = False, help: str = "") -> dict[str, Any]:
    return {"key": key, "label": label, "type": kind, "placeholder": placeholder,
            "secret": secret, "help": help}


mp.register(ProviderSpec(
    key="matrix",
    label="MiniMax (Matrix connector)",
    capabilities=(mp.IMAGE, mp.VIDEO, mp.SPEECH),
    kind="remote",
    doc="The MiniMax desktop runtime: images, video and speech through mcode-tools.",
    default_enabled=True,
), MatrixProvider)

mp.register(ProviderSpec(
    key="openai",
    label="OpenAI-compatible (any model)",
    capabilities=(mp.TEXT, mp.EMBEDDINGS, mp.IMAGE, mp.SPEECH),
    kind="remote",
    needs_key=False,
    doc=("Anything that speaks the OpenAI API: OpenAI, DeepSeek, Together, Groq, "
         "LM Studio, llama.cpp's server, vLLM, Ollama's /v1 endpoint."),
    default_config={"base_url": "http://127.0.0.1:1234/v1", "model": "",
                    "image_model": "gpt-image-1", "embed_model": "text-embedding-3-small"},
    config_fields=(
        _field("base_url", "Base URL", placeholder="http://127.0.0.1:1234/v1"),
        _field("api_key", "API key", kind="password", secret=True),
        _field("model", "Text model", placeholder="the model name for chat"),
        _field("image_model", "Image model", placeholder="gpt-image-1"),
        _field("embed_model", "Embedding model", placeholder="text-embedding-3-small"),
        _field("tts_model", "Speech model", placeholder="gpt-4o-mini-tts"),
    ),
), OpenAIProvider)

mp.register(ProviderSpec(
    key="ollama",
    label="Ollama (local)",
    capabilities=(mp.TEXT, mp.EMBEDDINGS),
    kind="local",
    doc="Models running on this Mac through Ollama. No key, no cost, offline.",
    default_config={"base_url": "http://127.0.0.1:11434", "model": "",
                    "embed_model": "nomic-embed-text"},
    config_fields=(
        _field("base_url", "Base URL", placeholder="http://127.0.0.1:11434"),
        _field("model", "Text model", placeholder="llama3.2"),
        _field("embed_model", "Embedding model", placeholder="nomic-embed-text"),
    ),
), OllamaProvider)

mp.register(ProviderSpec(
    key="comfyui",
    label="ComfyUI (local images & video)",
    capabilities=(mp.IMAGE, mp.VIDEO),
    kind="local",
    doc="ComfyUI on this Mac: images and video without leaving the machine.",
    default_config={"base_url": "http://127.0.0.1:8188", "prompt_node": "6",
                    "negative_node": "7", "workflow": "", "video_workflow": "",
                    "negative_prompt": ""},
    config_fields=(
        _field("base_url", "Base URL", placeholder="http://127.0.0.1:8188"),
        _field("workflow", "Image workflow (API JSON)", kind="textarea",
               placeholder='{"3": {"class_type": "KSampler", …}}'),
        _field("video_workflow", "Video workflow (API JSON)", kind="textarea",
               placeholder='{"3": {"class_type": "KSampler", …}}'),
        _field("prompt_node", "Prompt node id"),
        _field("negative_node", "Negative prompt node id"),
        _field("negative_prompt", "Negative prompt"),
    ),
), ComfyProvider)

mp.register(ProviderSpec(
    key="macos_say",
    label="macOS speech (local)",
    capabilities=(mp.SPEECH,),
    kind="local",
    doc="The synthesiser built into macOS: no dependency, no network, no key.",
    default_config={"voice": "Samantha"},
    config_fields=(_field("voice", "Voice", placeholder="Samantha"),),
), MacSayProvider)

mp.register(ProviderSpec(
    key="piper",
    label="Piper (local speech)",
    capabilities=(mp.SPEECH,),
    kind="local",
    doc="Piper: small local voices, either a piper-server or the binary.",
    default_config={"base_url": "", "binary": "piper", "voice_model": ""},
    config_fields=(
        _field("base_url", "Piper server URL", placeholder="http://127.0.0.1:5000"),
        _field("binary", "Binary", placeholder="piper"),
        _field("voice_model", "Voice model file", placeholder="/path/to/en_US-*.onnx"),
    ),
), PiperProvider)

mp.register(ProviderSpec(
    key="stability",
    label="Stability AI (remote images)",
    capabilities=(mp.IMAGE,),
    kind="remote",
    needs_key=True,
    doc="Stability AI: Core, SD3 or Ultra. The image comes back as bytes.",
    default_config={"base_url": "https://api.stability.ai", "model": "core"},
    config_fields=(
        _field("base_url", "Base URL", placeholder="https://api.stability.ai"),
        _field("api_key", "API key", kind="password", secret=True),
        _field("model", "Model", placeholder="core | sd3 | ultra"),
        _field("negative_prompt", "Negative prompt"),
    ),
), StabilityProvider)

mp.register(ProviderSpec(
    key="replicate",
    label="Replicate (remote images)",
    capabilities=(mp.IMAGE,),
    kind="remote",
    needs_key=True,
    doc="Replicate: any published model, as a prediction that is polled.",
    default_config={"base_url": "https://api.replicate.com", "model": "", "version": ""},
    config_fields=(
        _field("base_url", "Base URL", placeholder="https://api.replicate.com"),
        _field("api_key", "API key", kind="password", secret=True),
        _field("model", "Model", placeholder="owner/name"),
        _field("version", "Version (optional)"),
    ),
), ReplicateProvider)

mp.register(ProviderSpec(
    key="fal",
    label="fal.ai (remote images)",
    capabilities=(mp.IMAGE,),
    kind="remote",
    needs_key=True,
    doc="fal.ai: an endpoint per model, answering with a url.",
    default_config={"base_url": "https://fal.run", "model": ""},
    config_fields=(
        _field("base_url", "Base URL", placeholder="https://fal.run"),
        _field("api_key", "API key", kind="password", secret=True),
        _field("model", "Model", placeholder="fal-ai/flux/dev"),
    ),
), FalProvider)

mp.register(ProviderSpec(
    key="local_onnx",
    label="Local embeddings (ONNX)",
    capabilities=(mp.EMBEDDINGS,),
    kind="local",
    doc=("The same MiniLM model, run locally with ONNX Runtime: no torch, "
         "no network, no cost. Needs fastembed + onnxruntime installed."),
    default_config={"model": "sentence-transformers/all-MiniLM-L6-v2"},
    default_enabled=True,
    config_fields=(
        _field("model", "Model",
               placeholder="sentence-transformers/all-MiniLM-L6-v2",
               help=("all-MiniLM-L6-v2 is English and takes 86 MB. "
                     "jinaai/jina-embeddings-v2-base-es also handles Spanish "
                     "and takes 612 MB.")),
    ),
), LocalEmbedProvider)
