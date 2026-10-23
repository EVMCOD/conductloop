"""Providers: who does the work.

The product does not know about vendors. It asks for a **capability**
("image", "video", "speech", "text", "embeddings") and gets a provider that
can serve it: a cloud API, a local ComfyUI, an OpenAI-compatible server
running on this same Mac, or the MiniMax connector that came first.

That distinction matters. The first version of the media engine was one
vendor wearing the costume of "the engine", so every product decision —
which model, which key, which fallback — was a decision about MiniMax. The
logic is the part worth keeping, so the logic is what lives here.

A provider declares:

* the capabilities it can serve;
* whether it needs a key;
* the configuration it needs (base url, key, model names).

Configuration is stored in the database (``media_providers``), never in the
code, so adding a vendor is a row and a class — not a fork.

Adding a provider
-----------------
1. Write a subclass of :class:`Provider` implementing the capabilities it
   claims. Implement only those; the base raises a clear error for the rest.
2. Register it in :data:`BACKENDS`.
3. Declare it in :data:`PROVIDER_SPECS` with its capabilities.

The product picks it up automatically: the registry, the API and the page
all read the same declaration.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

# ── the capabilities the product speaks ─────────────────────────────────────

IMAGE = "image"
VIDEO = "video"
SPEECH = "speech"
TEXT = "text"
EMBEDDINGS = "embeddings"

CAPABILITIES = (IMAGE, VIDEO, SPEECH, TEXT, EMBEDDINGS)

CAPABILITY_LABELS = {
    IMAGE: "Image",
    VIDEO: "Video",
    SPEECH: "Speech",
    TEXT: "Text",
    EMBEDDINGS: "Embeddings",
}


@dataclass(frozen=True)
class ProviderSpec:
    """What a provider is and what it can do."""

    key: str
    label: str
    capabilities: tuple[str, ...]
    kind: str = "remote"                # "remote" | "local"
    needs_key: bool = False
    doc: str = ""
    default_config: dict[str, Any] = field(default_factory=dict)
    # The configuration the UI must ask for, and what each field means.
    config_fields: tuple[dict[str, Any], ...] = ()
    # Whether the provider is used without the user turning it on. Only the
    # one that needs no setup at all gets this: everything else asks first,
    # because "enabled" is a claim about the user's money and their machine.
    default_enabled: bool = False

    def to_dict(self, *, enabled: bool = False, configured: bool = False) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "capabilities": list(self.capabilities),
            "needs_key": self.needs_key,
            "doc": self.doc,
            "enabled": enabled,
            "configured": configured,
            "default_enabled": self.default_enabled,
            "config_fields": [dict(f) for f in self.config_fields],
            "default_config": {k: v for k, v in self.default_config.items()
                               if k not in ("api_key", "token")},
        }


class MediaError(RuntimeError):
    """Any failure in the media pipeline, with a message fit for the UI.

    ``code`` carries the engine's own error code when there is one. It is the
    only way to tell "this account cannot pay for that model" (402) from a
    transient hiccup, and the product needs that distinction to fall back
    instead of failing the user's render.
    """

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


# One name for the same thing: a provider that cannot do it.
ProviderError = MediaError


@dataclass
class MediaResult:
    """Outcome of one media operation."""

    ok: bool
    kind: str = ""                      # image | video | audio
    path: str | None = None             # local file, when there is one
    node_id: str | None = None
    task_id: str | None = None          # video: the external task id
    url: str | None = None              # short-lived source url
    status: str = "ready"               # ready | queued | running | succeeded | failed
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # Which provider did it: the product shows it, and the cost is booked
    # against it.
    provider: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "kind": self.kind, "path": self.path,
            "node_id": self.node_id, "task_id": self.task_id, "url": self.url,
            "status": self.status, "meta": self.meta, "error": self.error,
            "provider": self.provider,
        }


class Provider:
    """Base class for every backend.

    A provider implements only the capabilities it declares. The others
    raise a clear error instead of failing somewhere deep.
    """

    spec: ProviderSpec

    def __init__(self, config: dict[str, Any] | None = None):
        self.spec = type(self).spec
        self.config: dict[str, Any] = dict(self.spec.default_config)
        self.config.update(config or {})

    # ── configuration helpers ────────────────────────────────────────────

    def get(self, key: str, default: Any = "") -> Any:
        value = self.config.get(key, default)
        return default if value in (None, "") else value

    @property
    def api_key(self) -> str:
        return str(self.get("api_key", "") or "").strip()

    @property
    def base_url(self) -> str:
        return str(self.get("base_url", "") or "").strip().rstrip("/")

    @property
    def identity(self) -> str:
        """The name this provider is recorded under.

        Usually the provider key. An adapter around an older engine keeps
        the engine's own name instead, so an asset still says who really
        made it.
        """
        return self.spec.key

    # ── capability API ───────────────────────────────────────────────────

    def generate_image(self, prompt: str, **kw) -> Any:
        raise ProviderError(f"{self.spec.label} cannot generate images")

    def submit_video(self, prompt: str, **kw) -> Any:
        raise ProviderError(f"{self.spec.label} cannot generate video")

    def poll_video(self, task_id: str, **kw) -> Any:
        raise ProviderError(f"{self.spec.label} cannot poll video jobs")

    def synthesize_speech(self, text: str, **kw) -> Any:
        raise ProviderError(f"{self.spec.label} cannot synthesize speech")

    def embed(self, texts: list[str], **kw) -> list[list[float]]:
        raise ProviderError(f"{self.spec.label} cannot embed text")

    def chat(self, messages: list[dict], **kw) -> str:
        raise ProviderError(f"{self.spec.label} cannot chat")

    # ── what this provider can do ─────────────────────────────────────────
    #
    # The product builds its forms from these answers instead of from a
    # hardcoded table of somebody else's models. A provider that knows less
    # than the product would say "anything goes"; one that knows more says
    # exactly which combinations it bills for.

    def video_options(self) -> dict[str, Any]:
        """The video models this provider offers, and their rules."""
        return {
            "models": [],
            "default_model": "",
            "fast_model": "",
            "fallback_model": "",
            "accepts_ratio": True,
            "keyframe_modes": ["first_frame", "last_frame", "subject"],
            "references": False,
        }

    def default_video_model(self) -> str:
        options = self.video_options()
        models = options.get("models") or []
        return options.get("default_model") or (models[0]["model"] if models else "")

    def draft_video_model(self) -> str:
        """The model to use for a quick draft.

        Some providers publish a cheaper model for throwaway renders. The
        product asks for the draft model instead of naming one, so the rule
        stays where the models are described.
        """
        options = self.video_options()
        return options.get("fallback_model") or self.default_video_model()

    def validate_video(self, *, model: str | None = None, duration: int = 6,
                       resolution: str | None = None, ratio: str | None = None,
                       reference_type: str | None = None,
                       has_image: bool = False) -> None:
        """Reject a render this provider cannot actually make.

        The generic rules are deliberately forgiving: a provider that does
        not publish its own table gets the benefit of the doubt, because a
        wrong rejection costs the user a render they could have paid for.
        """
        options = self.video_options()
        models = options.get("models") or []
        if models and model and model not in {m.get("model") for m in models}:
            raise ProviderError(
                f"{self.spec.label} does not offer '{model}'; "
                f"available: {', '.join(m.get('model', '') for m in models)}"
            )
        durations = options.get("durations") or []
        if durations and int(duration) not in durations:
            raise ProviderError(
                f"{self.spec.label} takes {durations[0]}-{durations[-1]} seconds, "
                f"not {duration}"
            )
        resolutions = options.get("resolutions") or []
        if resolution and resolutions and resolution not in resolutions:
            raise ProviderError(
                f"{self.spec.label} does not take {resolution}; "
                f"use {' or '.join(resolutions)}"
            )
        modes = options.get("keyframe_modes") or []
        if reference_type and modes and reference_type not in modes:
            raise ProviderError(
                f"{self.spec.label} has no '{reference_type}' mode; it takes "
                f"{', '.join(modes) or 'no keyframe'}"
            )

    def health(self) -> tuple[bool, str]:
        """Is this provider usable right now?"""
        return True, "ok"

    def embedding_tag(self) -> str:
        """The tag stored with this provider's vectors.

        Vectors are only ever compared with vectors of the same tag: two
        models have different dimensions and different meanings, and mixing
        them gives similarities that mean nothing.
        """
        return f"{self.spec.key}:{self.get('embed_model', '')}"

    # ── reporting ────────────────────────────────────────────────────────

    def describe(self) -> dict[str, Any]:
        return self.spec.to_dict(enabled=True, configured=self.configured())

    def configured(self) -> bool:
        if not self.spec.needs_key:
            return True
        return bool(self.api_key)


# ── the registry ───────────────────────────────────────────────────────────

PROVIDER_SPECS: tuple[ProviderSpec, ...] = ()
BACKENDS: dict[str, type[Provider]] = {}


def register(spec: ProviderSpec, backend: type[Provider]) -> None:
    """Declare a provider: what it is, and the class that implements it."""
    global PROVIDER_SPECS
    backend.spec = spec
    BACKENDS[spec.key] = backend
    if not any(s.key == spec.key for s in PROVIDER_SPECS):
        PROVIDER_SPECS = PROVIDER_SPECS + (spec,)


def spec_for(key: str) -> ProviderSpec | None:
    _ensure_backends()
    return next((s for s in PROVIDER_SPECS if s.key == key), None)


_BACKENDS_LOADED = False


def _ensure_backends() -> None:
    """Make sure every backend has registered itself.

    The backends live in their own module to keep this one free of vendor
    code, but the registry has to be complete before anybody asks it a
    question — otherwise a provider silently does not exist.
    """
    global _BACKENDS_LOADED
    if _BACKENDS_LOADED:
        return
    _BACKENDS_LOADED = True
    try:
        from . import media_backends  # noqa: F401  (registration side effect)
    except Exception:
        # A broken backend must not take the whole registry down with it.
        _BACKENDS_LOADED = False


def specs_for(capability: str) -> list[ProviderSpec]:
    """Every provider that can serve this capability."""
    _ensure_backends()
    return [s for s in PROVIDER_SPECS if capability in s.capabilities]


# ── configuration store ────────────────────────────────────────────────────

def _dbmod():
    from . import db_media
    return db_media


def stored_configs() -> dict[str, dict[str, Any]]:
    """The provider configuration, as stored."""
    try:
        rows = _dbmod().list_provider_configs()
    except Exception:
        return {}
    return {r["key"]: r for r in rows}


def build(key: str) -> Provider:
    """Instantiate a provider with its stored configuration."""
    spec = spec_for(key)
    if spec is None:
        raise ProviderError(f"unknown provider '{key}'")
    backend = BACKENDS.get(key)
    if backend is None:
        raise ProviderError(f"provider '{key}' has no implementation")
    stored = stored_configs().get(key) or {}
    config = dict(stored.get("config") or {})
    return backend(config)


def enabled_keys(capability: str | None = None) -> list[str]:
    """The enabled providers, best first.

    The order is the one the product uses when the user does not choose:
    explicit priority first, then **local before remote** — running the work
    on this machine is cheaper, faster and private, so when a local provider
    is set up it wins — then the order the providers were declared in.
    """
    _ensure_backends()
    rows = stored_configs()
    order = {spec.key: i for i, spec in enumerate(PROVIDER_SPECS)}
    out: list[tuple[tuple[int, int, int], str]] = []
    for spec in PROVIDER_SPECS:
        row = rows.get(spec.key)
        if row is not None:
            if not row.get("enabled"):
                continue
            # A provider nobody configured and that needs a key is not on.
            if spec.needs_key and not (row.get("config") or {}).get("api_key"):
                continue
            priority = int(row.get("priority") or 100)
        else:
            if not spec.default_enabled:
                continue
            priority = 100
        if capability and capability not in spec.capabilities:
            continue
        out.append(((priority, 0 if spec.kind == "local" else 1, order[spec.key]),
                    spec.key))
    return [key for _, key in sorted(out, key=lambda pair: pair[0])]


def pick(capability: str, provider_key: str | None = None) -> Provider:
    """The provider that will serve this capability.

    An explicit key wins. Otherwise the first enabled provider for the
    capability is used, and a clear error says what is available when
    nothing is.
    """
    if provider_key:
        provider = build(provider_key)
        if capability not in provider.spec.capabilities:
            raise ProviderError(
                f"{provider.spec.label} does not do {CAPABILITY_LABELS.get(capability, capability)}"
            )
        return provider
    for key in enabled_keys(capability):
        try:
            provider = build(key)
        except ProviderError:
            continue
        if provider.configured():
            return provider
    available = ", ".join(s.key for s in specs_for(capability)) or "none"
    raise ProviderError(
        f"no provider is set up for {CAPABILITY_LABELS.get(capability, capability)}. "
        f"Add one in Settings; available: {available}"
    )


def catalogue() -> list[dict[str, Any]]:
    """Every provider, with its state — what the settings page shows."""
    _ensure_backends()
    rows = stored_configs()
    out = []
    for spec in PROVIDER_SPECS:
        row = rows.get(spec.key) or {}
        config = row.get("config") or {}
        enabled = bool(row.get("enabled")) if row else spec.default_enabled
        entry = spec.to_dict(
            enabled=enabled,
            configured=(bool(config.get("api_key")) if spec.needs_key else True),
        )
        # The page shows what is actually stored, not what the defaults are.
        # The secret never travels back.
        entry["default_config"] = {
            **entry["default_config"],
            **{k: v for k, v in config.items() if k not in ("api_key", "token")},
        }
        out.append(entry)
    return out


def set_enabled(key: str, enabled: bool) -> None:
    """Turn a provider on or off, keeping whatever it is configured with."""
    if spec_for(key) is None:
        raise ProviderError(f"unknown provider '{key}'")
    _dbmod().set_provider_state(key, enabled=bool(enabled))


# ── what the work costs ───────────────────────────────────────────────────

def daily_cap(provider: Provider) -> int:
    """The most calls this provider may make today. Zero means no cap."""
    try:
        return max(0, int(provider.get("daily_cap", 0) or 0))
    except (TypeError, ValueError):
        return 0


def guard_spend(provider: Provider) -> None:
    """Refuse the call when the provider is already at its daily cap.

    The cap exists because a render that costs money should stop when the
    user says so, not when the provider's own limit says so — and because
    the count is visible before the limit is reached.
    """
    cap = daily_cap(provider)
    if cap <= 0:
        return
    day = time.strftime("%Y-%m-%d")
    used = _dbmod().provider_usage_since(provider.spec.key, day)
    if used >= cap:
        raise ProviderError(
            f"{provider.spec.label} has used its daily budget: {used}/{cap} calls. "
            f"Raise the cap or wait until tomorrow."
        )


@contextlib.contextmanager
def counted(provider: Provider):
    """Count the call against the provider, whatever the outcome."""
    guard_spend(provider)
    ok = True
    try:
        yield
    except Exception:
        ok = False
        raise
    finally:
        try:
            _dbmod().record_provider_usage(provider.spec.key, ok=ok)
        except Exception:
            # A missing counter must never fail the work itself.
            pass


def capability_map() -> dict[str, list[str]]:
    """Which providers can serve which capability."""
    return {cap: [s.key for s in specs_for(cap)] for cap in CAPABILITIES}


# ── helpers shared by the HTTP backends ────────────────────────────────────

def http_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
              payload: Any = None, timeout: float = 120.0) -> Any:
    """A small JSON client that does not drag a dependency into the app."""
    import urllib.error
    import urllib.request

    body = None
    head = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        head.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=head, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise ProviderError(f"HTTP {e.code} from {url}: {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise ProviderError(f"could not reach {url}: {e}") from e
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProviderError(f"{url} did not answer with JSON") from e


def http_bytes(url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
               payload: Any = None, timeout: float = 120.0) -> bytes:
    """Fetch raw bytes — for an image or an audio file.

    ``payload`` may be bytes (a multipart body, an audio blob) or a string.
    """
    import urllib.error
    import urllib.request

    head = dict(headers or {})
    body = None
    if payload is not None:
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode("utf-8")
            head.setdefault("Content-Type", "application/json")
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = bytes(payload)
        head.setdefault("Content-Type", "application/octet-stream")
    req = urllib.request.Request(url, data=body, headers=head, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise ProviderError(f"HTTP {e.code} from {url}: {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise ProviderError(f"could not reach {url}: {e}") from e


def save_config(key: str, config: dict[str, Any], *, enabled: bool | None = None,
                priority: int | None = None) -> None:
    """Store a provider's configuration."""
    _dbmod().set_provider_config(key, config, enabled=enabled, priority=priority)


def describe_all() -> list[dict[str, Any]]:
    """Provider states, with a live check for the enabled ones."""
    _ensure_backends()
    rows = stored_configs()
    out = []
    for spec in PROVIDER_SPECS:
        row = rows.get(spec.key) or {}
        enabled = bool(row.get("enabled")) if row else spec.default_enabled
        entry = spec.to_dict(
            enabled=enabled,
            configured=(bool((row.get("config") or {}).get("api_key"))
                        if spec.needs_key else True),
        )
        stored = row.get("config") or {}
        entry["default_config"] = {
            **entry["default_config"],
            **{k: v for k, v in stored.items() if k not in ("api_key", "token")},
        }
        if entry["enabled"]:
            try:
                provider = build(spec.key)
                ok, detail = provider.health()
                entry["healthy"] = ok
                entry["detail"] = detail
            except Exception as e:
                entry["healthy"] = False
                entry["detail"] = str(e)[:200]
        out.append(entry)
    return out
