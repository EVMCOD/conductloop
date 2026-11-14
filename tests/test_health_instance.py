"""/api/health echoes the desktop app's instance id in full.

The Tauri shell (desktop/src-tauri/src/lib.rs) generates a 64-hex-char id and
waits for a /api/health body that contains it before it hands the boot nonce
to the server. A shortened echo never matched, so the window gave up after
~2.5 minutes, loaded the dashboard without the nonce and showed a 403.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ops_dashboard import routes_meta

LIB_RS = Path(__file__).resolve().parent.parent / "desktop" / "src-tauri" / "src" / "lib.rs"


class _Handler:
    def _send(self, code, body, ctype):
        self.code, self.body = code, json.loads(body)


def _health():
    h = _Handler()
    routes_meta.health(h)
    return h.body


def test_the_whole_instance_id_is_echoed(monkeypatch):
    instance = "ab" * 32  # what random_hex32() yields: 32 bytes as hex
    monkeypatch.setenv("CONDUCTLOOP_INSTANCE_ID", instance)
    monkeypatch.setenv("CONDUCTLOOP_APP_TOKEN", "t" * 64)
    body = _health()
    assert body["instance"] == instance
    assert instance in json.dumps(body)  # the shell's check is a substring match


def test_no_instance_without_the_app_token(monkeypatch):
    monkeypatch.setenv("CONDUCTLOOP_INSTANCE_ID", "ab" * 32)
    monkeypatch.delenv("CONDUCTLOOP_APP_TOKEN", raising=False)
    assert "instance" not in _health()


def test_the_shell_still_matches_the_whole_id():
    src = LIB_RS.read_text()
    assert re.search(r"let mut buf = \[0u8; 32\];", src), "random_hex32 changed size"
    assert "body.contains(id.as_str())" in src, "the shell's health check changed"
