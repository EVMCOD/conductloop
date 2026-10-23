"""Image-to-image and the prompt presets.

Text-to-image that ignores a reference image is just text-to-image with an
extra step, and a bare sentence is a weak instruction. Both of those are
things the product has to get right, so they get tests.
"""
from __future__ import annotations

import pytest

from ops_dashboard import media


class _Engine(media.MatrixEngine):
    """Captures the request the engine would send; never calls anything."""

    def __init__(self):
        self._cli = "/bin/true"
        self.uploaded: list[str] = []
        self.requests: list[dict] = []

    def upload(self, path, *, mime_type=None):
        self.uploaded.append(str(path))
        return "https://engine.invalid/tmp/ref.png"

    def _call(self, tool, args, **kw):
        self.requests.append(args)
        # One success item per requested image, like the real engine.
        n = len(args.get("requests") or [])
        return {"code": 0, "success_items": [
            {"node_id": f"n{i}", "file_name": f"{i}.png"} for i in range(max(n, 1))]}


@pytest.fixture
def eng(monkeypatch, tmp_path):
    from ops_dashboard import db
    e = _Engine()
    # Install it in the registry: replacing set_engine would leave
    # get_engine() building the real CLI-backed engine and calling the API.
    media.set_engine(e)
    monkeypatch.setattr(media.app_paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(media.MatrixEngine, "_download",
                        lambda self, url, dest: _write(dest))
    monkeypatch.setattr(media.MatrixEngine, "_asset_url",
                        lambda self, node_id: "https://engine.invalid/a.png")
    db.init_db()
    yield e
    media.set_engine(None)


def _write(dest):
    from pathlib import Path
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"\x89PNG\r\n\x1a\nx")
    return dest


def _ref(tmp_path, name="ref.png"):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\nref")
    return str(p)


# ── image-to-image ─────────────────────────────────────────────────────────

def test_a_reference_image_is_uploaded_and_sent_as_input_urls(tmp_path, eng):
    media.generate_image("make it blue", image_path=_ref(tmp_path))
    assert len(eng.uploaded) == 1
    requests = eng.requests[0]["requests"]
    assert requests[0]["input_urls"] == ["https://engine.invalid/tmp/ref.png"]


def test_the_reference_is_uploaded_once_for_a_batch_of_variations(tmp_path, eng):
    """Four variations share one reference; four uploads would be four calls."""
    media.generate_image("more like this", image_path=_ref(tmp_path), n=4)
    assert len(eng.requests[0]["requests"]) == 4
    assert len(eng.uploaded) == 1


def test_text_to_image_sends_no_reference(tmp_path, eng):
    media.generate_image("a plain scene")
    assert "input_urls" not in eng.requests[0]["requests"][0]
    assert eng.uploaded == []


def test_a_variation_is_recorded_with_its_reference(tmp_path, eng):
    res = media.generate_image("more like this", image_path=_ref(tmp_path))
    assert res.meta["variation"] is True
    assert res.meta["reference"].endswith("ref.png")


def test_a_missing_reference_is_refused_before_the_engine_is_called(tmp_path, eng):
    with pytest.raises(media.MediaError, match="reference image not found"):
        media.generate_image("x", image_path=str(tmp_path / "gone.png"))
    assert eng.requests == []


def test_an_unsupported_reference_format_is_refused(tmp_path, eng):
    gif = tmp_path / "a.gif"
    gif.write_bytes(b"GIF89a")
    with pytest.raises(media.MediaError, match="unsupported reference format"):
        media.generate_image("x", image_path=str(gif))
    assert eng.requests == []


def test_a_relative_reference_path_is_resolved(tmp_path, eng, monkeypatch):
    """Same trap as the keyframe upload: the CLI runs in another directory."""
    _ref(tmp_path)
    monkeypatch.chdir(tmp_path)
    media.generate_image("more like this", image_path="ref.png")
    assert eng.uploaded[0] == str((tmp_path / "ref.png").resolve())


# ── presets ────────────────────────────────────────────────────────────────

def test_every_preset_produces_a_complete_prompt():
    """A prompt with a hole in it would render something nobody asked for."""
    for preset in media.IMAGE_PRESETS:
        out = media.apply_preset(preset.key, "a brass compass")
        assert "{" not in out["prompt"] and "}" not in out["prompt"]
        assert "a brass compass" in out["prompt"]
        assert len(out["prompt"]) > 80, f"{preset.key} prompt is too thin"


def test_a_preset_supplies_the_aspect_ratio_it_was_designed_for():
    assert media.apply_preset("app_icon", "an orb")["aspect_ratio"] == "1:1"
    assert media.apply_preset("landing_hero", "an orb")["aspect_ratio"] == "16:9"


def test_an_explicit_aspect_ratio_wins_over_the_preset_default():
    out = media.apply_preset("landing_hero", "an orb", aspect_ratio="9:16")
    assert out["aspect_ratio"] == "9:16"


def test_an_unknown_preset_says_what_is_available():
    with pytest.raises(media.MediaError, match="app_icon"):
        media.apply_preset("nope", "an orb")


def test_a_preset_needs_a_subject():
    with pytest.raises(media.MediaError, match="subject is required"):
        media.apply_preset("app_icon", "   ")


def test_the_presets_are_reachable_by_the_client():
    presets = media.prompt_presets()
    assert {p["key"] for p in presets} == {p.key for p in media.IMAGE_PRESETS}
    for p in presets:
        assert {"key", "label", "hint", "aspect_ratio", "prompt"} <= set(p)


def test_presets_are_advertised_in_the_status():
    assert media.engine_status()["image_presets"]


# ── variations are a batch, not a single image ─────────────────────────────

def test_variations_default_to_a_useful_batch_size(tmp_path, eng):
    """Asking for "variations" and getting one back is not a variation."""
    from ops_dashboard import db_media

    res = media.generate_image("more like this", image_path=_ref(tmp_path), n=4)
    assert len(res.meta["paths"]) == 4

# ── the background is a choice, not an accident ────────────────────────────

def test_the_icon_preset_offers_its_own_backgrounds():
    """Leaving the background to the model produced pastel, neon, white and
    even transparent icons for the same preset. 2026-10-23."""
    out = media.apply_preset("app_icon", "a compass")
    assert "solid" in out["prompt"]
    assert "opaque" in out["prompt"]
    assert "no transparency" in out["prompt"]
    assert media.IMAGE_PRESETS[0].backgrounds


def test_the_chosen_background_goes_into_the_prompt():
    out = media.apply_preset("app_icon", "a compass", background="deep midnight navy")
    assert "deep midnight navy" in out["prompt"]


def test_an_unknown_background_is_refused():
    with pytest.raises(media.MediaError, match="background must be one of"):
        media.apply_preset("app_icon", "a compass", background="hot pink")


def test_a_preset_without_backgrounds_ignores_the_parameter():
    out = media.apply_preset("landing_hero", "a desk", background="deep midnight navy")
    assert "deep midnight navy" not in out["prompt"]


def test_the_hero_preset_forbids_visible_brand_logos():
    """A generated hero came back with a DELL badge on the monitor. 2026-10-23."""
    out = media.apply_preset("landing_hero", "a trading desk")
    assert "no visible brand logos" in out["prompt"]
    assert "unbranded" in out["prompt"]


def test_the_background_reaches_the_client():
    for preset in media.prompt_presets():
        assert "backgrounds" in preset
