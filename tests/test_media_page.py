"""Static checks for the media page and the media endpoints' contracts.

The page renders whatever the media library holds — prompts, file names,
model names — so it builds its DOM with nodes and never with markup strings.
That is easy to undo by accident, hence the sink checks below.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ops_dashboard import db_media, media

WEB = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web"
HTML = (WEB / "media.html").read_text(encoding="utf-8")


def _script() -> str:
    """The page's JavaScript, without the CSS block and the HTML comments."""
    body = re.search(r"<script>\n(.*?)\n</script>", HTML, re.S)
    assert body, "the page has no inline script"
    return body.group(1)


# ── no markup sinks ────────────────────────────────────────────────────────

@pytest.mark.parametrize("pattern", [
    r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"document\.write",
    r"\beval\s*\(", r"new\s+Function\b",
])
def test_the_page_never_writes_markup_from_library_text(pattern):
    """A prompt is user-authored: pasting markup into one must not execute."""
    script = re.sub(r"^\s*//.*$", "", _script(), flags=re.M)
    assert not re.search(pattern, script), pattern


@pytest.mark.parametrize("pattern", [r"\son[a-z]+\s*=", r"\sstyle\s*="])
def test_the_page_has_no_inline_handlers_or_styles(pattern):
    """CSP is style-src 'self'; an inline style attribute would be dropped."""
    markup = re.sub(r"<script>.*?</script>", "", HTML, flags=re.S)
    assert not re.search(pattern, markup, re.I), pattern


def test_the_page_loads_the_shared_design_system_and_the_csrf_echo():
    assert '<link rel="stylesheet" href="/theme.css">' in HTML
    assert '<script src="/csrf.js" defer></script>' in HTML
    # theme.css first, so the page's own rules can build on its tokens.
    assert HTML.index("/theme.css") < HTML.index("<style>")


def test_the_page_does_not_invent_its_own_token_names():
    """--bg / --ink / --brand were the old vocabulary; they must not creep back."""
    assert not re.search(r"var\(--(bg|ink|card|muted|brand|border|red|blue|ok)\b", HTML)


def test_no_api_key_material_reaches_the_page():
    assert not re.search(r"api_key|apiKey|bearer\s+[A-Za-z0-9]", HTML, re.I)


# ── the form only offers what the engine accepts ───────────────────────────

def test_the_form_is_built_from_the_engine_capabilities():
    """Durations and resolutions come from /api/media/status, not hardcoded."""
    script = _script()
    assert "video_capabilities" in script
    assert "resolutions_by_duration" in script
    assert "fill($(\"vidDur\"), c.durations)" in script
    assert "native_audio" in script


def test_a_keyframe_locks_the_aspect_ratio():
    """The engine follows the keyframe's aspect; offering a ratio is a lie."""
    script = _script()
    assert "locked" in script
    assert "accepts_ratio" in script


def test_the_default_animate_prompt_matches_the_server_default():
    from ops_dashboard import routes_media
    # Same first clause in both places, so the button and the API agree.
    head = routes_media.ANIMATE_DEFAULT_PROMPT.split(",")[0].strip()
    assert head in _script()


def test_the_keyframe_controls_start_in_the_empty_state():
    """"Clear" on an empty keyframe box is a control that does nothing."""
    script = _script()
    assert "setKeyframe(null)" in script, "boot must reset the keyframe state"
    # The markup itself must not ship the button visible.
    assert re.search(r'id="kfClear"[^>]*hidden', HTML), "kfClear must start hidden"
    assert re.search(r'id="kfImg"[^>]*hidden', HTML), "kfImg must start hidden"


# ── the endpoints behind the page ──────────────────────────────────────────

@pytest.fixture
def server(tmp_path, monkeypatch):
    from ops_dashboard import db
    from ops_dashboard.server import DashboardHandler

    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(media.app_paths, "data_dir", lambda: tmp_path)
    db.init_db()

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
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def _get(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=20) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


def test_the_page_is_served(server):
    status, body, ctype = _get(server, "/media")
    assert status == 200
    assert "text/html" in ctype
    assert b"Media" in body


def test_the_presets_endpoint_lists_what_the_page_renders(server):
    status, body, _ = _get(server, "/api/media/presets")
    assert status == 200
    presets = json.loads(body)["presets"]
    assert len(presets) == len(media.IMAGE_PRESETS)


def test_assets_expose_a_url_the_gallery_can_use(server, tmp_path):
    """The gallery points at /api/media/file/<id>, never at a path on disk."""
    png = tmp_path / "media" / "p" / "a.png"
    png.parent.mkdir(parents=True)
    png.write_bytes(b"\x89PNG\r\n\x1a\nx")
    asset_id = db_media.add_asset(kind="image", prompt="x", path=str(png), status="ready")

    _, body, _ = _get(server, "/api/media/assets")
    row = json.loads(body)["rows"][0]
    assert row["url"] == f"/api/media/file/{asset_id}"
    assert "path" in row  # still returned for the API, just not what the page uses

    status, data, ctype = _get(server, f"/api/media/file/{asset_id}")
    assert status == 200
    assert ctype == "image/png"
    assert data.startswith(b"\x89PNG")


def test_a_missing_asset_is_a_clean_404_not_a_path_error(server):
    status, _body, _ = _get(server, "/api/media/file/4242")
    assert status == 404


def test_the_file_endpoint_serves_audio_and_video_types(server, tmp_path):
    for name, ctype, payload in (("a.mp3", "audio/mpeg", b"ID3"),
                                 ("a.mp4", "video/mp4", b"\x00\x00\x00 ftypmp42")):
        f = tmp_path / "media" / "p" / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(payload)
        aid = db_media.add_asset(kind="audio", prompt="x", path=str(f), status="ready")
        status, data, got = _get(server, f"/api/media/file/{aid}")
        assert status == 200, name
        assert got == ctype


def test_an_unknown_extension_is_served_as_an_octet_stream(server, tmp_path):
    f = tmp_path / "media" / "p" / "a.bin"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\x01\x02")
    aid = db_media.add_asset(kind="image", prompt="x", path=str(f), status="ready")
    status, _data, got = _get(server, f"/api/media/file/{aid}")
    assert status == 200
    assert got == "application/octet-stream"

# ── deleting an asset ──────────────────────────────────────────────────────

def _png(tmp_path, name="a.png"):
    p = tmp_path / "media" / "p" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\nx")
    return str(p)


def _delete(base: str, path: str):
    req = urllib.request.Request(base + path, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_deleting_an_asset_removes_the_row_and_the_file(server, tmp_path):
    from pathlib import Path
    png = _png(tmp_path)
    aid = db_media.add_asset(kind="image", prompt="x", path=png, status="ready")
    status, body = _delete(server, f"/api/media/assets/{aid}")
    assert status == 200, body
    assert body["file_removed"] is True
    assert not Path(png).exists()
    assert db_media.get_asset(aid) is None


def test_deleting_an_unknown_asset_is_a_404(server):
    status, _ = _delete(server, "/api/media/assets/9999")
    assert status == 404


def test_deleting_a_bad_id_is_a_400(server):
    status, _ = _delete(server, "/api/media/assets/abc")
    assert status == 400


def test_a_row_whose_file_is_already_gone_still_deletes_cleanly(server, tmp_path):
    from pathlib import Path
    png = _png(tmp_path)
    aid = db_media.add_asset(kind="image", prompt="x", path=png, status="ready")
    Path(png).unlink()
    status, body = _delete(server, f"/api/media/assets/{aid}")
    assert status == 200, body
    assert body["file_removed"] is False
    assert db_media.get_asset(aid) is None


def test_delete_refuses_a_row_pointing_outside_the_library(server, tmp_path):
    """A tampered row must not make the endpoint delete an arbitrary file."""
    outside = tmp_path.parent / "zz-outside.png"
    outside.write_bytes(b"precious")
    aid = db_media.add_asset(kind="image", prompt="x", path=str(outside), status="ready")
    status, body = _delete(server, f"/api/media/assets/{aid}")
    assert status == 200, body
    assert body["file_removed"] is False
    assert outside.exists(), "the file outside the library must survive"


# ── the provider picker ────────────────────────────────────────────────────

def test_the_page_offers_a_provider_per_capability():
    """The user chooses who does the work; the product still asks for a
    capability, so the picker is filled from the server's catalogue."""
    html = HTML
    assert 'id="imgProvider"' in html
    assert 'id="vidProvider"' in html
    assert 'id="varyProvider"' in html
    script = _script()
    assert "/api/media/providers" in script
    assert "capabilities" in script


def test_the_providers_panel_lets_a_backend_be_configured():
    html = HTML
    assert 'id="pane-providers"' in html
    assert 'id="providerList"' in html
    script = _script()
    assert "config_fields" in script
    assert "Turn on" in script and "Turn off" in script


def test_the_provider_is_sent_with_the_render():
    script = _script()
    assert 'provider: $("imgProvider").value' in script
    assert 'provider: $("vidProvider").value' in script


def test_an_empty_api_key_keeps_the_stored_one():
    """The field starts empty on purpose: the stored secret is never sent
    back to the page, so an empty field means "leave it alone"."""
    script = _script()
    assert 'if (i.value || i.dataset.key === "daily_cap")' in script
    assert "i.value;" in script
