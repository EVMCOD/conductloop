"""Unit test for web/provider-errors.js (the shared UI helper for the 503
``provider_unavailable`` payload). Runs the helper under Node with a stub
``navigator``; skipped when Node isn't installed."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parent.parent / "ops_dashboard" / "web" / "provider-errors.js"
NODE = shutil.which("node")

_SCRIPT = r"""
const path = process.argv[1];
const out = {};
for (const language of ['es-ES', 'en-US']) {
  // Node >= 21 ships a read-only global navigator; override it for the test.
  Object.defineProperty(globalThis, 'navigator', { value: { language }, configurable: true, writable: true });
  delete require.cache[require.resolve(path)];
  const PE = require(path);
  const payload = { ok: false, error: 'provider_unavailable', code: 'provider_unavailable',
                    provider: 'anthropic', model: 'claude-x', retry_after: 590,
                    reason: 'cooldown', message: 'Provider anthropic failed recently' };
  const el = { textContent: '', style: {}, dataset: {}, attrs: {},
               setAttribute(k, v) { this.attrs[k] = v; } };
  out[language] = {
    message: PE.message('openai'),
    fromPayload: PE.friendly(payload),
    fromDoneEvent: PE.friendly({ type: 'done', error: 'x', provider_unavailable: payload }),
    fromText: PE.friendly('provider_unavailable:minimax — Provider minimax failed'),
    passthrough: PE.friendly('RuntimeError: boom'),
    other503: PE.providerOf({ error: 'rate limited', code: 'rate_limited' }),
    shown: PE.show(el, payload),
    el: el.textContent, role: el.attrs.role,
  };
}
(async () => {
  const PE = require(path);
  const resp = (status, body) => ({ status, clone() { return { json: async () => body }; } });
  out.fromResponse503 = (await PE.fromResponse(resp(503, { error: 'provider_unavailable', provider: 'openai' }))).provider;
  out.fromResponse500 = await PE.fromResponse(resp(500, { error: 'provider_unavailable', provider: 'openai' }));
  out.fromResponseOther503 = await PE.fromResponse(resp(503, { code: 'provider_down' }));
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_provider_errors_helper_messages():
    res = subprocess.run([NODE, "-e", _SCRIPT, str(HELPER)], capture_output=True,
                         text=True, timeout=30, check=True)
    out = json.loads(res.stdout)
    es, en = out["es-ES"], out["en-US"]
    assert es["message"] == "El proveedor openai no está disponible ahora mismo, elige otro"
    assert en["message"] == "Provider openai is unavailable right now, choose another one"
    assert es["fromPayload"] == "El proveedor anthropic no está disponible ahora mismo, elige otro"
    assert en["fromPayload"] == "Provider anthropic is unavailable right now, choose another one"
    assert en["fromDoneEvent"] == en["fromPayload"]
    assert en["fromText"] == "Provider minimax is unavailable right now, choose another one"
    assert en["passthrough"] == "RuntimeError: boom"
    assert en["other503"] is None
    for lang in (es, en):
        assert lang["shown"] is True and lang["role"] == "alert"
        assert "503" not in lang["el"] and "provider_unavailable" not in lang["el"]
    assert en["el"] == "⚠ Provider anthropic is unavailable right now, choose another one"
    assert out["fromResponse503"] == "openai"
    assert out["fromResponse500"] is None and out["fromResponseOther503"] is None
