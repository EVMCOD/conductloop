"""Every error the app reports has to be visible in the app.

The desktop window is served over http by the local server, and Tauri v2
overrides `window.alert` with an IPC call to the dialog plugin. Without the
permission for it, the call is refused and the message goes nowhere: the
action appears to do nothing and the user is told the app is broken.

There were 47 `alert()` calls in the shell, so this was not one bad message
but every message the app can show. They now go to a toast in the page,
which works in the app and in a browser, and the dialog permission is
granted as well so the remaining `confirm()` / `prompt()` calls work.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "ops_dashboard" / "web"
CAPABILITIES = ROOT / "desktop" / "src-tauri" / "capabilities"
NODE = shutil.which("node")


def test_the_shell_does_not_use_alert():
    """alert() is a no-op in the desktop app."""
    src = (WEB / "app.js").read_text(encoding="utf-8")
    assert "alert(" not in src, (
        "the shell still uses alert(), which the desktop app ignores: the "
        "message never reaches the user")


def test_the_toast_is_loaded_before_the_shell():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "toast.js" in html, "the toast helper is not loaded"
    assert html.index("toast.js") < html.index("/app.js"), (
        "the toast helper has to be loaded before the shell uses it")


def test_the_shell_is_valid_javascript():
    """A syntax error in the shell is a blank app, and nothing says so."""
    assert NODE is not None, "node is not installed"
    chk = subprocess.run([NODE, "--check", str(WEB / "app.js")],
                         capture_output=True, text=True, timeout=60)
    assert chk.returncode == 0, chk.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_toast_shows_the_message_in_the_page():
    """The message has to end up in the DOM, not in a native dialog."""
    src = (WEB / "toast.js").read_text(encoding="utf-8")
    script = """
const shown = [];
const fake = (tag) => ({
  tagName: tag, className: '', textContent: '', _attrs: {}, children: [],
  setAttribute(k, v) { this._attrs[k] = v; },
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
  removeChild(c) { this.children = this.children.filter(x => x !== c); },
  getAttribute(k) { return this._attrs[k]; },
});
const body = fake('body');
global.document = {
  body: body,
  getElementById(id) { return id === 'toast-root' ? body : null; },
  createElement(tag) { return fake(tag); },
};
global.setTimeout = () => 0;
global.window = global;
__SRC__
const el = window.notify('boom', 'error');
process.stdout.write(JSON.stringify({
  text: el && el.textContent,
  cls: el && el.className,
  role: el && el.getAttribute('role'),
  inBody: !!(el && el.parentNode),
}));
""".replace("__SRC__", src)
    run = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["text"] == "boom", out
    assert "error" in (out["cls"] or ""), out
    assert out["role"] == "alert", out
    assert out["inBody"], out


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_an_empty_message_shows_nothing():
    src = (WEB / "toast.js").read_text(encoding="utf-8")
    script = """
const fake = (tag) => ({ tagName: tag, className: '', textContent: '', children: [],
  setAttribute() {}, appendChild(c) { this.children.push(c); return c; }, removeChild() {} });
const body = fake('body');
global.document = { body: body, getElementById: () => body, createElement: (t) => fake(t) };
global.setTimeout = () => 0;
global.window = global;
__SRC__
const el = window.notify('');
process.stdout.write(JSON.stringify({shown: !!el}));
""".replace("__SRC__", src)
    run = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["shown"] is False
