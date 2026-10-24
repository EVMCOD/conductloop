"""The provider framework: one contract, many vendors, local or remote.

The point of this file is the claim the product makes: *"ask for a
capability, not for a vendor"*. These tests exercise that claim against a
mock server that speaks both the OpenAI API and Ollama's own, so the same
code path is proven for a cloud API and for a model running on this Mac.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ops_dashboard import media
from ops_dashboard import media_providers as mp
from ops_dashboard.media_providers import MediaError

# ── a mock that speaks both API dialects ──────────────────────────────────


class _MockHandler(BaseHTTPRequestHandler):
    """Answers like an OpenAI-compatible server *and* like Ollama."""

    calls: list[tuple[str, str, dict]] = []

    def log_message(self, *args):        # keep the test output clean
        pass

    def _read(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _send(self, payload, *, raw: bytes | None = None, content_type="application/json"):
        body = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).calls.append(("GET", self.path, {}))
        if self.path.endswith("/models"):
            self._send({"data": [{"id": "test-model"}]})
        elif self.path.endswith("/api/tags"):
            self._send({"models": [{"name": "llama3.2"}, {"name": "nomic-embed-text"}]})
        else:
            self._send({"ok": True})

    def do_POST(self):
        body = self._read()
        type(self).calls.append(("POST", self.path, body))
        path = self.path

        if path.endswith("/chat/completions"):
            self._send({"choices": [{"message": {"content": "hola desde el mock"}}]})
        elif path.endswith("/api/embeddings"):
            self._send({"embedding": [0.7, 0.8, 0.9]})
        elif path.endswith("/embeddings"):
            self._send({"data": [{"embedding": [0.1, 0.2, 0.3]},
                                 {"embedding": [0.4, 0.5, 0.6]}]})
        elif path.endswith("/images/generations"):
            blob = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-image").decode()
            self._send({"data": [{"b64_json": blob}]})
        elif path.endswith("/audio/speech"):
            self._send(None, raw=b"ID3fake-mp3-audio", content_type="audio/mpeg")
        elif path.endswith("/api/chat"):
            self._send({"message": {"content": "hola desde ollama"}})
        else:
            self._send({"ok": True})


@pytest.fixture(scope="module")
def mock_server():
    _MockHandler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def clean_registry():
    """The tests share one database: leave no provider behind."""
    from ops_dashboard import db

    db.init_db()
    yield
    with db.db() as conn:
        conn.execute("DELETE FROM media_providers")


def _enable(key: str, **config):
    mp.save_config(key, config, enabled=True)
    return key


# ── the registry ───────────────────────────────────────────────────────────

def test_every_capability_has_at_least_one_provider():
    """A capability nobody can serve is a dead end in the product."""
    catalogue = mp.catalogue()
    assert catalogue, "no provider is registered"
    for capability in mp.CAPABILITIES:
        assert mp.specs_for(capability), f"no provider for {capability}"


def test_the_minimax_connector_stays_on_by_default():
    """It is the one that needs no setup, so it is the one that works."""
    assert "matrix" in mp.enabled_keys("image")
    assert "matrix" in mp.enabled_keys("video")
    assert "matrix" in mp.enabled_keys("speech")


def test_a_provider_that_needs_a_key_is_off_until_it_has_one():
    mp.save_config("stability", {"api_key": "", "base_url": "https://api.stability.ai"},
                   enabled=True)
    assert "stability" not in mp.enabled_keys("image")


def test_a_local_provider_is_preferred_over_a_remote_one():
    """Work done on this machine is free, fast and private."""
    _enable("openai", base_url="http://127.0.0.1:1/v1", model="m")
    _enable("ollama", base_url="http://127.0.0.1:1", model="m")
    assert mp.enabled_keys("text")[0] == "ollama"


def test_an_explicit_priority_beats_everything():
    _enable("openai", base_url="http://127.0.0.1:1/v1", model="m")
    _enable("ollama", base_url="http://127.0.0.1:1", model="m")
    mp.save_config("openai", {}, enabled=True, priority=1)
    assert mp.enabled_keys("text")[0] == "openai"


def test_a_disabled_provider_is_not_used():
    _enable("ollama", base_url="http://127.0.0.1:1", model="m")
    mp.set_enabled("ollama", False)
    assert "ollama" not in mp.enabled_keys("text")


def test_picking_a_provider_that_cannot_do_the_job_is_an_error():
    with pytest.raises(MediaError) as exc:
        mp.pick("video", "ollama")
    assert "does not do" in str(exc.value).lower()


def test_picking_with_nothing_available_says_what_is_missing():
    """A clear error beats a stack trace in the UI."""
    with pytest.raises(MediaError) as exc:
        mp.pick("text", None)
    message = str(exc.value)
    assert "no provider is set up" in message.lower()
    assert "available" in message.lower()


# ── any model, behind an OpenAI-compatible API ────────────────────────────

def test_one_class_serves_every_openai_compatible_model(mock_server):
    """OpenAI, DeepSeek, Together, LM Studio, llama.cpp, vLLM: same class."""
    provider = mp.build("openai")
    provider.config["base_url"] = f"{mock_server}/v1"
    provider.config["model"] = "some/model"

    assert provider.chat([{"role": "user", "content": "hola"}]) == "hola desde el mock"
    vectors = provider.embed(["uno", "dos"])
    assert len(vectors) == 2 and vectors[0] == [0.1, 0.2, 0.3]


def test_the_openai_compatible_provider_generates_an_image(mock_server, tmp_path):
    provider = mp.build("openai")
    provider.config["base_url"] = f"{mock_server}/v1"
    provider.config["image_model"] = "gpt-image-1"

    res = provider.generate_image("un gato", project_key="demo")
    assert res.ok and res.path
    assert os.path.isfile(res.path)
    assert res.provider == "openai"


def test_the_openai_compatible_provider_speaks(mock_server):
    provider = mp.build("openai")
    provider.config["base_url"] = f"{mock_server}/v1"
    provider.config["tts_model"] = "tts-1"

    res = provider.synthesize_speech("hola", project_key="demo")
    assert res.ok and os.path.isfile(res.path)
    assert res.kind == "audio"


def test_the_openai_compatible_provider_reports_its_health(mock_server):
    provider = mp.build("openai")
    provider.config["base_url"] = f"{mock_server}/v1"
    ok, detail = provider.health()
    assert ok is True
    assert mock_server in detail


def test_an_unreachable_provider_says_so_instead_of_crashing():
    provider = mp.build("openai")
    provider.config["base_url"] = "http://127.0.0.1:9/v1"
    ok, detail = provider.health()
    assert ok is False
    assert detail


# ── a model running on this Mac ───────────────────────────────────────────

def test_ollama_serves_text_and_embeddings_locally(mock_server):
    provider = mp.build("ollama")
    provider.config["base_url"] = mock_server

    assert provider.chat([{"role": "user", "content": "hola"}]) == "hola desde ollama"
    assert provider.embed(["uno"]) == [[0.7, 0.8, 0.9]]

    ok, detail = provider.health()
    assert ok is True
    assert "model" in detail.lower()


def test_ollama_is_not_enabled_until_it_is_asked_for():
    assert "ollama" not in mp.enabled_keys("text")
    _enable("ollama", base_url="http://127.0.0.1:1")
    assert "ollama" in mp.enabled_keys("text")


# ── the macOS speech synthesiser ──────────────────────────────────────────

@pytest.mark.skipif(not shutil.which("say"), reason="macOS speech synthesiser is missing")
def test_the_local_speech_synthesiser_actually_speaks(tmp_path, monkeypatch):
    """A backend that claims to work has to produce a file that is not empty."""
    monkeypatch.setenv("OPS_DATA_DIR", str(tmp_path))
    provider = mp.build("macos_say")

    res = provider.synthesize_speech("prueba local", project_key="demo")
    assert res.ok
    assert os.path.isfile(res.path)
    assert os.path.getsize(res.path) > 1000
    assert res.provider == "macos_say"


# ── the product asks for a capability, not for a vendor ───────────────────

def test_the_product_uses_the_provider_it_is_given(monkeypatch):
    """The seam: one provider, used for every kind of work."""
    calls: list[str] = []

    class _Fake(mp.Provider):
        spec = mp.ProviderSpec(key="fake", label="Fake",
                               capabilities=(mp.IMAGE, mp.VIDEO, mp.SPEECH))

        def generate_image(self, prompt, **kw):
            calls.append("image")
            path = media.media_dir("provtest") / "fake.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            return mp.MediaResult(ok=True, kind="image", path=str(path),
                                  provider="fake", meta={"paths": [str(path)]})

    # Another test file leaves a forced engine behind; the provider path is
    # what this test is about.
    monkeypatch.setattr(media, "_ENGINE", None)
    monkeypatch.setattr(mp, "pick", lambda capability, key=None: _Fake())
    res = media.generate_image("un gato", project_key="provtest")
    assert res.ok
    assert calls == ["image"]
    assert res.meta["provider"] == "fake"


def test_the_product_records_which_provider_made_an_asset(monkeypatch):
    from ops_dashboard import db

    class _Fake(mp.Provider):
        spec = mp.ProviderSpec(key="fake", label="Fake", capabilities=(mp.IMAGE,))

        def generate_image(self, prompt, **kw):
            path = media.media_dir("provtest") / "fake2.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            return mp.MediaResult(ok=True, kind="image", path=str(path),
                                  provider="fake", meta={"paths": [str(path)]})

    monkeypatch.setattr(media, "_ENGINE", None)
    monkeypatch.setattr(mp, "pick", lambda capability, key=None: _Fake())
    media.generate_image("un gato", project_key="provtest")
    rows = db.list_assets(project_key="provtest")
    assert rows and rows[0]["engine"] == "fake"


# ── the configuration is a secret ─────────────────────────────────────────

def test_the_api_key_is_never_handed_back():
    _enable("stability", api_key="sk-secret-value")
    catalogue = mp.catalogue()
    entry = next(c for c in catalogue if c["key"] == "stability")
    assert "sk-secret-value" not in json.dumps(catalogue)
    assert entry["configured"] is True


def test_saving_an_empty_key_keeps_the_one_that_is_stored():
    """An empty key means "keep the one I have" — and the one I have is in
    the vault, not in the database."""
    _enable("stability", api_key="sk-secret-value")
    mp.save_config("stability", {"api_key": "", "model": "sd3"}, enabled=True)
    stored = mp.stored_configs()["stability"]["config"]
    assert "api_key" not in stored
    assert stored["model"] == "sd3"
    assert mp.build("stability").api_key == "sk-secret-value"


# ── what the work costs ───────────────────────────────────────────────────

def test_a_call_is_counted_against_its_provider():
    from ops_dashboard import db_media

    class _Fake(mp.Provider):
        spec = mp.ProviderSpec(key="counted", label="Counted", capabilities=(mp.IMAGE,))

        def generate_image(self, prompt, **kw):
            return mp.MediaResult(ok=True, kind="image", provider="counted",
                                  meta={"paths": []})

    mp.register(_Fake.spec, _Fake)
    provider = mp.build("counted")
    with mp.counted(provider):
        pass
    usage = db_media.provider_usage_today()
    assert usage["counted"]["calls"] == 1


def test_a_failed_call_is_counted_as_a_failure():
    from ops_dashboard import db_media

    class _Fake(mp.Provider):
        spec = mp.ProviderSpec(key="failing", label="Failing", capabilities=(mp.IMAGE,))

    mp.register(_Fake.spec, _Fake)
    provider = mp.build("failing")
    with pytest.raises(RuntimeError):
        with mp.counted(provider):
            raise RuntimeError("boom")
    usage = db_media.provider_usage_today()
    assert usage["failing"]["calls"] == 1
    assert usage["failing"]["errors"] == 1


def test_the_daily_cap_stops_the_work():
    """A provider that spends money has to stop when the user says so."""
    class _Fake(mp.Provider):
        spec = mp.ProviderSpec(key="capped", label="Capped", capabilities=(mp.IMAGE,))

    mp.register(_Fake.spec, _Fake)
    provider = mp.build("capped")
    provider.config["daily_cap"] = "1"
    with mp.counted(provider):
        pass
    with pytest.raises(MediaError) as exc:
        mp.guard_spend(provider)
    assert "daily budget" in str(exc.value)


def test_no_cap_means_no_limit():
    class _Fake(mp.Provider):
        spec = mp.ProviderSpec(key="uncapped", label="Uncapped", capabilities=(mp.IMAGE,))

    mp.register(_Fake.spec, _Fake)
    provider = mp.build("uncapped")
    provider.config["daily_cap"] = "0"
    mp.guard_spend(provider)          # must not raise


def test_a_missing_fastembed_is_not_retried_on_every_call():
    """The packaged app has no fastembed: remembering that keeps every
    embedding call from walking a 175 MB path it does not have."""
    from ops_dashboard import media_backends

    media_backends._FASTEMBED_UNTIL = 0.0
    media_backends._FASTEMBED_MESSAGE = ""
    provider = mp.build("local_onnx")
    try:
        provider.embed(["hola"])
        return  # fastembed is installed here; there is nothing to remember
    except MediaError:
        pass
    assert media_backends._FASTEMBED_UNTIL > time.time()
    # The second call must fail the same way, not with some other error: a
    # blind "raises Exception" would pass even if the reason were wrong.
    started = time.time()
    for _ in range(10):
        with pytest.raises(MediaError) as exc:
            provider.embed(["hola"])
        assert "fastembed" in str(exc.value)
    assert time.time() - started < 1.0


# ── one endpoint for the whole product ─────────────────────────────────────

def test_the_media_provider_uses_the_server_the_product_is_pointed_at(monkeypatch):
    """The brain and the media pipeline talk to the same kind of server.

    Pointing ConductLoop at a local runtime once has to mean the chat and
    the images come from the same place, so the media provider inherits the
    url when it has none of its own.
    """
    from ops_dashboard import brain, byok_keys

    monkeypatch.setattr(brain, "_load_config", lambda: {"openai_base_url": "http://127.0.0.1:1234/v1"})
    monkeypatch.setattr(byok_keys, "get_api_key", lambda provider, cfg=None: "sk-local")

    provider = mp.build("openai")
    assert provider.base_url == "http://127.0.0.1:1234/v1"
    assert provider.api_key == "sk-local"


def test_its_own_base_url_wins(monkeypatch):
    from ops_dashboard import brain, byok_keys

    monkeypatch.setattr(brain, "_load_config", lambda: {"openai_base_url": "http://127.0.0.1:1234/v1"})
    monkeypatch.setattr(byok_keys, "get_api_key", lambda provider, cfg=None: "sk-local")

    provider = mp.build("openai")
    provider.config["base_url"] = "https://api.deepseek.com/v1"
    provider.config["api_key"] = "sk-own"
    assert provider.base_url == "https://api.deepseek.com/v1"
    assert provider.api_key == "sk-own"


def test_the_catalogue_says_where_the_work_goes(monkeypatch):
    from ops_dashboard import brain, byok_keys

    monkeypatch.setattr(brain, "_load_config", lambda: {"openai_base_url": "http://127.0.0.1:1234/v1"})
    monkeypatch.setattr(byok_keys, "get_api_key", lambda provider, cfg=None: "")
    entry = next(e for e in mp.catalogue() if e["key"] == "openai")
    assert entry["effective_base_url"] == "http://127.0.0.1:1234/v1"


def test_the_local_engine_is_used_when_torch_is_absent(monkeypatch):
    """The desktop app has no torch: the ONNX engine is the one that must work.

    It used to fall through to the remote API — which is rate limited — and
    index nothing, because the health check said "the model is not
    downloaded yet" and that was treated as "not available". The model is
    fetched on first use, so not having it is not a failure.

    The engine is faked here on purpose: this is about the decision, not
    about having the package installed.
    """
    import sys
    import types

    monkeypatch.setitem(sys.modules, "fastembed", types.ModuleType("fastembed"))
    provider = mp.build("local_onnx")
    ok, detail = provider.health()
    assert ok is True, detail
    assert "fetched on first use" in detail or "ready" in detail


def test_changing_the_model_embeds_the_documents_again(tmp_path, monkeypatch):
    """The vectors of two models are not comparable.

    Keeping the old ones would leave the search answering with the previous
    model's view of the world. What matters is that the document is *chosen*
    again, so the embedding itself is faked: this is about the selection.
    """
    import json

    from ops_dashboard import db, embeddings

    monkeypatch.setenv("CONDUCTLOOP_DB_PATH", str(tmp_path / "emb.db"))
    db.DB_PATH = tmp_path / "emb.db"  # type: ignore[attr-defined]
    db.init_db()
    embeddings.ensure_schema()
    db.upsert_document(source="obsidian", path="/v/a.md", title="A",
                       content="un gato duerme", metadata={}, mtime=1, size=10)
    with db.db() as conn:
        doc_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
        # A vector from another model.
        conn.execute(
            "INSERT INTO embeddings (doc_id, vector, model, indexed_at) VALUES (?,?,?,?)",
            (doc_id, json.dumps([0.1, 0.2]), "un-otro-modelo", 1))
        conn.commit()

    chosen: list[int] = []
    monkeypatch.setattr(embeddings, "index_document",
                        lambda i, c, **kw: chosen.append(i))
    embeddings.run()
    assert chosen == [doc_id], "the document must be embedded again"
